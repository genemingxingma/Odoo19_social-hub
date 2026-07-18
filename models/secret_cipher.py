import logging
import os
import re

from cryptography.fernet import Fernet, InvalidToken

from odoo import _
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)

_ENV_KEY = 'SOCIAL_HUB_ENCRYPTION_KEY'
_IDENTIFIER_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


def _get_cipher(required=True):
    key = (os.getenv(_ENV_KEY) or '').strip()
    if not key:
        if required:
            raise UserError(_(
                'Social Hub encryption is not configured. Set the '
                'SOCIAL_HUB_ENCRYPTION_KEY environment variable and restart Odoo.'
            ))
        return False
    try:
        return Fernet(key.encode('ascii'))
    except (ValueError, UnicodeEncodeError) as exc:
        if required:
            raise UserError(_(
                'SOCIAL_HUB_ENCRYPTION_KEY is invalid. Generate a Fernet key and restart Odoo.'
            )) from exc
        return False


def encrypt_secret(value):
    if not value:
        return False
    return _get_cipher().encrypt(value.encode('utf-8')).decode('ascii')


def decrypt_secret(value):
    if not value:
        return False
    try:
        return _get_cipher().decrypt(value.encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise UserError(_(
            'A Social Hub secret cannot be decrypted. Verify the server encryption key.'
        )) from exc


def migrate_legacy_secrets(env, table, field_mapping):
    """Move legacy plaintext columns into encrypted storage during module upgrade."""
    identifiers = [table, *field_mapping.keys(), *field_mapping.values()]
    if not all(_IDENTIFIER_RE.fullmatch(name) for name in identifiers):
        raise ValueError('Unsafe database identifier in Social Hub secret migration.')

    env.cr.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
        """,
        (table,),
    )
    columns = {row[0] for row in env.cr.fetchall()}
    cipher = _get_cipher(required=False)

    for plaintext_column, encrypted_column in field_mapping.items():
        if plaintext_column not in columns or encrypted_column not in columns:
            continue
        env.cr.execute(
            f"""
            SELECT id, {plaintext_column}
              FROM {table}
             WHERE {plaintext_column} IS NOT NULL
               AND {plaintext_column} != ''
               AND ({encrypted_column} IS NULL OR {encrypted_column} = '')
            """
        )
        legacy_rows = env.cr.fetchall()
        if legacy_rows and not cipher:
            _logger.error(
                'Cannot migrate %s.%s: %s is not configured.',
                table,
                plaintext_column,
                _ENV_KEY,
            )
            continue
        for record_id, plaintext_value in legacy_rows:
            encrypted_value = cipher.encrypt(plaintext_value.encode('utf-8')).decode('ascii')
            env.cr.execute(
                f"""
                UPDATE {table}
                   SET {encrypted_column} = %s,
                       {plaintext_column} = NULL
                 WHERE id = %s
                """,
                (encrypted_value, record_id),
            )

