import logging
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import requests

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .secret_cipher import decrypt_secret, encrypt_secret, migrate_legacy_secrets


_logger = logging.getLogger(__name__)

_DEFAULT_META_SCOPES = (
    'pages_show_list,pages_read_engagement,pages_manage_posts,'
    'instagram_basic,instagram_content_publish'
)


class SocialHubAccount(models.Model):
    _name = 'social.hub.account'
    _description = 'Social Hub Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'platform_id, name'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    platform_id = fields.Many2one(
        'social.hub.platform',
        required=True,
        ondelete='restrict',
        tracking=True,
    )
    platform_code = fields.Selection(related='platform_id.code', store=True, index=True)
    handle = fields.Char(required=True, help='For example: @brand_official', tracking=True)
    external_uid = fields.Char(
        string='Platform Asset ID',
        help='Facebook Page ID or Instagram professional account ID. Set this before sync when several assets are available.',
    )
    profile_url = fields.Char()

    access_token_encrypted = fields.Text(
        copy=False,
        groups='social_hub.group_social_hub_manager',
    )
    access_token = fields.Char(
        compute='_compute_access_token',
        inverse='_inverse_access_token',
        groups='social_hub.group_social_hub_manager',
    )
    token_expires_at = fields.Datetime(groups='social_hub.group_social_hub_manager')
    meta_user_access_token_encrypted = fields.Text(
        copy=False,
        groups='social_hub.group_social_hub_manager',
    )
    meta_user_access_token = fields.Char(
        compute='_compute_meta_user_access_token',
        inverse='_inverse_meta_user_access_token',
        groups='social_hub.group_social_hub_manager',
    )
    meta_user_token_expires_at = fields.Datetime(groups='social_hub.group_social_hub_manager')
    meta_last_refresh_at = fields.Datetime(groups='social_hub.group_social_hub_manager')

    state = fields.Selection(
        [('draft', 'Draft'), ('connected', 'Connected'), ('disconnected', 'Disconnected')],
        default='draft',
        tracking=True,
        index=True,
    )
    oauth_provider = fields.Selection(
        [('meta', 'Meta')],
        groups='social_hub.group_social_hub_manager',
    )
    oauth_state = fields.Char(
        groups='social_hub.group_social_hub_manager',
        copy=False,
        index=True,
    )
    oauth_state_expires_at = fields.Datetime(groups='social_hub.group_social_hub_manager')

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        domain=lambda self: [('id', 'in', self.env.companies.ids)],
        index=True,
        tracking=True,
    )
    note = fields.Text()
    last_sync_at = fields.Datetime(readonly=True)

    stream_ids = fields.One2many('social.hub.stream', 'account_id')
    stream_count = fields.Integer(compute='_compute_stream_count')

    _account_handle_unique = models.Constraint(
        'UNIQUE(platform_id, handle, company_id)',
        'This handle already exists for this platform and company.',
    )

    def init(self):
        migrate_legacy_secrets(
            self.env,
            self._table,
            {
                'access_token': 'access_token_encrypted',
                'meta_user_access_token': 'meta_user_access_token_encrypted',
            },
        )

    @api.model_create_multi
    def create(self, vals_list):
        prepared_values = []
        for values in vals_list:
            values = dict(values)
            if 'access_token' in values:
                values['access_token_encrypted'] = encrypt_secret(values.pop('access_token'))
            if 'meta_user_access_token' in values:
                values['meta_user_access_token_encrypted'] = encrypt_secret(
                    values.pop('meta_user_access_token')
                )
            prepared_values.append(values)
        return super().create(prepared_values)

    def write(self, values):
        values = dict(values)
        if 'access_token' in values:
            values['access_token_encrypted'] = encrypt_secret(values.pop('access_token'))
        if 'meta_user_access_token' in values:
            values['meta_user_access_token_encrypted'] = encrypt_secret(
                values.pop('meta_user_access_token')
            )
        return super().write(values)

    def _compute_access_token(self):
        for account in self:
            account.access_token = decrypt_secret(account.access_token_encrypted)

    def _inverse_access_token(self):
        for account in self:
            account.access_token_encrypted = encrypt_secret(account.access_token)

    def _compute_meta_user_access_token(self):
        for account in self:
            account.meta_user_access_token = decrypt_secret(account.meta_user_access_token_encrypted)

    def _inverse_meta_user_access_token(self):
        for account in self:
            account.meta_user_access_token_encrypted = encrypt_secret(account.meta_user_access_token)

    def _compute_stream_count(self):
        for account in self:
            account.stream_count = len(account.stream_ids)

    @api.constrains('handle')
    def _check_handle(self):
        for record in self:
            if not record.handle or len(record.handle.strip()) < 2:
                raise ValidationError(_('Handle must be at least 2 characters.'))

    def _check_social_hub_manager(self):
        if not self.env.user.has_group('social_hub.group_social_hub_manager'):
            raise AccessError(_('Only Social Hub managers can manage account connections.'))

    def action_mark_connected(self):
        self._check_social_hub_manager()
        for account in self:
            if account.platform_code in ('facebook', 'instagram'):
                if not account.external_uid or not account.access_token:
                    raise UserError(_('Connect Meta OAuth before marking this account as connected.'))
        self.write({'state': 'connected', 'last_sync_at': fields.Datetime.now()})

    def action_mark_disconnected(self):
        self._check_social_hub_manager()
        self.write({'state': 'disconnected'})

    def action_connect_meta(self):
        self.ensure_one()
        self._check_social_hub_manager()
        if self.platform_code not in ('facebook', 'instagram'):
            raise UserError(_('Meta OAuth is only available for Facebook and Instagram accounts.'))

        conf = self._get_meta_conf()
        if not conf['app_id']:
            raise UserError(_('Please set Meta App ID in Social Hub settings first.'))

        state = secrets.token_urlsafe(32)
        self.write({
            'oauth_provider': 'meta',
            'oauth_state': state,
            'oauth_state_expires_at': fields.Datetime.now() + timedelta(minutes=15),
        })

        params = {
            'client_id': conf['app_id'],
            'redirect_uri': self._meta_redirect_uri(),
            'state': state,
            'response_type': 'code',
            'scope': conf['scopes'],
        }
        oauth_url = f"https://www.facebook.com/{conf['version']}/dialog/oauth?{urlencode(params)}"
        return {
            'type': 'ir.actions.act_url',
            'url': oauth_url,
            'target': 'self',
        }

    def action_sync_meta_assets(self):
        self.ensure_one()
        self._check_social_hub_manager()
        if self.platform_code not in ('facebook', 'instagram'):
            raise UserError(_('Meta sync is only available for Facebook and Instagram accounts.'))

        user_token = self.meta_user_access_token or self.access_token
        if not user_token:
            raise UserError(_('No token found. Please connect Meta OAuth first.'))
        self._meta_sync_from_user_access_token(user_token)

    def action_refresh_meta_token(self):
        self._check_social_hub_manager()
        for account in self:
            if account.platform_code not in ('facebook', 'instagram'):
                continue
            account._meta_refresh_user_access_token(force=True)
            account._meta_sync_from_user_access_token(
                account.meta_user_access_token or account.access_token
            )

    def _get_meta_conf(self):
        company = self.company_id if self else self.env.company
        conf = self.env['social.hub.meta.config'].sudo().search(
            [('company_id', '=', company.id), ('active', '=', True)],
            limit=1,
        )
        if not conf:
            return {
                'app_id': '',
                'app_secret': '',
                'version': 'v25.0',
                'scopes': _DEFAULT_META_SCOPES,
            }
        return {
            'app_id': conf.meta_app_id or '',
            'app_secret': conf.meta_app_secret or '',
            'version': conf.meta_graph_version or 'v25.0',
            'scopes': conf.meta_scopes or _DEFAULT_META_SCOPES,
        }

    def _meta_redirect_uri(self):
        base_url = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise UserError(_('Odoo web.base.url must be configured before Meta OAuth.'))
        if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1'):
            raise UserError(_('Meta OAuth requires an HTTPS web.base.url.'))
        return f'{base_url}/social_hub/oauth/meta/callback'

    def _meta_graph_base(self):
        return f"https://graph.facebook.com/{self._get_meta_conf()['version']}"

    def _meta_request(self, method, path, *, params=None, data=None, token=None, timeout=30):
        self.ensure_one()
        method = method.upper()
        url = path if path.startswith('https://') else f"{self._meta_graph_base()}/{path.lstrip('/')}"
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'graph.facebook.com':
            raise UserError(_('Refusing a Meta API request to an unexpected host.'))

        request_params = dict(params or {})
        request_data = dict(data or {})
        if token:
            target = request_params if method == 'GET' else request_data
            target['access_token'] = token

        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                response = requests.request(
                    method,
                    url,
                    params=request_params or None,
                    data=request_data or None,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise UserError(_('Meta API connection failed. Please try again later.')) from exc

            try:
                result = response.json()
            except ValueError:
                result = {}

            if response.status_code < 400 and not result.get('error'):
                return result

            if method == 'GET' and response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                retry_after = response.headers.get('Retry-After')
                delay = min(10, int(retry_after)) if retry_after and retry_after.isdigit() else 2 ** attempt
                time.sleep(delay)
                continue

            error = result.get('error') or {}
            code = error.get('code') or response.status_code
            message = error.get('message') or _('Unexpected response from Meta.')
            raise UserError(_('Meta API request failed (%s): %s') % (code, message))

        raise UserError(_('Meta API request failed.'))

    def _meta_exchange_and_sync(self, code):
        self.ensure_one()
        conf = self._get_meta_conf()
        if not conf['app_id'] or not conf['app_secret']:
            raise UserError(_('Meta App ID / App Secret are required in settings.'))

        token_data = self._meta_request(
            'GET',
            'oauth/access_token',
            params={
                'client_id': conf['app_id'],
                'client_secret': conf['app_secret'],
                'redirect_uri': self._meta_redirect_uri(),
                'code': code,
            },
        )
        short_token = token_data.get('access_token')
        expires_in = int(token_data.get('expires_in') or 0)
        if not short_token:
            raise UserError(_('Meta token exchange returned no access token.'))

        self.write({
            'meta_user_access_token': short_token,
            'meta_user_token_expires_at': (
                fields.Datetime.now() + timedelta(seconds=expires_in) if expires_in else False
            ),
        })
        self._meta_refresh_user_access_token(force=True)
        self._meta_sync_from_user_access_token(self.meta_user_access_token or short_token)

    def _meta_refresh_user_access_token(self, force=False):
        self.ensure_one()
        if self.platform_code not in ('facebook', 'instagram') or not self.meta_user_access_token:
            return False
        if not force and self.meta_user_token_expires_at:
            if self.meta_user_token_expires_at > fields.Datetime.now() + timedelta(days=10):
                return False

        conf = self._get_meta_conf()
        if not conf['app_id'] or not conf['app_secret']:
            raise UserError(_('Meta App ID / App Secret are required in settings.'))

        refresh_data = self._meta_request(
            'GET',
            'oauth/access_token',
            params={
                'grant_type': 'fb_exchange_token',
                'client_id': conf['app_id'],
                'client_secret': conf['app_secret'],
                'fb_exchange_token': self.meta_user_access_token,
            },
        )
        new_token = refresh_data.get('access_token')
        expires_in = int(refresh_data.get('expires_in') or 0)
        if not new_token:
            raise UserError(_('Meta token refresh returned no access token.'))

        self.write({
            'meta_user_access_token': new_token,
            'meta_user_token_expires_at': (
                fields.Datetime.now() + timedelta(seconds=expires_in) if expires_in else False
            ),
            'meta_last_refresh_at': fields.Datetime.now(),
        })
        return True

    def _meta_get_pages(self, user_access_token):
        fields_list = (
            'id,name,access_token,link,'
            'instagram_business_account{id,username,name,profile_picture_url}'
        )
        url = f'{self._meta_graph_base()}/me/accounts'
        params = {'fields': fields_list, 'limit': 100}
        pages = []
        for _page_number in range(20):
            payload = self._meta_request(
                'GET',
                url,
                params=params,
                token=user_access_token if params else None,
            )
            pages.extend(payload.get('data') or [])
            next_url = (payload.get('paging') or {}).get('next')
            if not next_url:
                break
            parsed = urlparse(next_url)
            if parsed.scheme != 'https' or parsed.hostname != 'graph.facebook.com':
                raise UserError(_('Meta returned an unsafe pagination URL.'))
            url = next_url
            params = None
        return pages

    def _select_meta_asset(self, candidates, label):
        if self.external_uid:
            selected = next((candidate for candidate in candidates if candidate['id'] == self.external_uid), False)
            if selected:
                return selected
            raise UserError(_('%s ID %s is not available to the connected Meta user.') % (
                label,
                self.external_uid,
            ))
        if len(candidates) == 1:
            return candidates[0]
        options = ', '.join(
            f"{candidate.get('name') or candidate['id']} ({candidate['id']})"
            for candidate in candidates[:20]
        )
        raise UserError(_(
            'Several %s assets are available. Enter the desired Platform Asset ID and sync again: %s'
        ) % (label, options))

    def _meta_sync_from_user_access_token(self, user_access_token):
        self.ensure_one()
        self._meta_request(
            'GET',
            'me',
            params={'fields': 'id,name'},
            token=user_access_token,
        )
        pages = self._meta_get_pages(user_access_token)
        if not pages:
            raise UserError(_('No Facebook Pages are available for this user token.'))

        if self.platform_code == 'facebook':
            page = self._select_meta_asset(pages, _('Facebook Page'))
            page_id = page.get('id')
            self.write({
                'name': page.get('name') or self.name,
                'external_uid': page_id,
                'profile_url': page.get('link') or (f'https://www.facebook.com/{page_id}' if page_id else False),
                'access_token': page.get('access_token') or user_access_token,
                'state': 'connected',
                'last_sync_at': fields.Datetime.now(),
            })
            return

        instagram_assets = []
        for page in pages:
            instagram = page.get('instagram_business_account')
            if instagram:
                instagram_assets.append({
                    **instagram,
                    'source_page_token': page.get('access_token') or user_access_token,
                })
        if not instagram_assets:
            raise UserError(_('No Instagram professional account was found in the available Facebook Pages.'))

        instagram = self._select_meta_asset(instagram_assets, _('Instagram account'))
        ig_id = instagram.get('id')
        ig_username = instagram.get('username')
        ig_name = instagram.get('name') or ig_username or self.name
        if ig_id and (not ig_username or not instagram.get('name')):
            details = self._meta_request(
                'GET',
                ig_id,
                params={'fields': 'id,username,name,profile_picture_url'},
                token=instagram['source_page_token'],
            )
            ig_username = details.get('username') or ig_username
            ig_name = details.get('name') or ig_name

        self.write({
            'name': ig_name,
            'handle': ig_username or self.handle,
            'external_uid': ig_id,
            'profile_url': f'https://www.instagram.com/{ig_username}/' if ig_username else False,
            'access_token': instagram['source_page_token'],
            'state': 'connected',
            'last_sync_at': fields.Datetime.now(),
        })

    @api.model
    def cron_refresh_meta_tokens(self):
        accounts = self.sudo().search([
            ('platform_code', 'in', ['facebook', 'instagram']),
            ('state', '=', 'connected'),
            ('meta_user_access_token_encrypted', '!=', False),
        ])
        self.env['ir.cron']._commit_progress(remaining=len(accounts))
        for account in accounts:
            try:
                refreshed = account._meta_refresh_user_access_token(force=False)
                if refreshed:
                    account._meta_sync_from_user_access_token(account.meta_user_access_token)
                    account.message_post(body=_('Meta token refreshed automatically.'))
            except Exception:
                _logger.exception('Automatic Meta token refresh failed for account %s.', account.id)
                vals = {}
                if account.meta_user_token_expires_at and account.meta_user_token_expires_at <= fields.Datetime.now():
                    vals['state'] = 'disconnected'
                if vals:
                    account.write(vals)
                account.message_post(body=_('Automatic Meta token refresh failed. Check the server log and reconnect if needed.'))
            if not self.env['ir.cron']._commit_progress(processed=1):
                break
