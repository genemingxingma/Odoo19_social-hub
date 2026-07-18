import os
from unittest.mock import patch

from cryptography.fernet import Fernet

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestSocialHub(TransactionCase):

    def setUp(self):
        super().setUp()
        self.key_patch = patch.dict(
            os.environ,
            {'SOCIAL_HUB_ENCRYPTION_KEY': Fernet.generate_key().decode('ascii')},
        )
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def test_secrets_are_encrypted_at_rest(self):
        config = self.env['social.hub.meta.config'].create({
            'name': 'Test Meta',
            'company_id': self.env.company.id,
            'meta_app_id': '123',
            'meta_app_secret': 'do-not-store-plaintext',
        })
        self.assertEqual(config.meta_app_secret, 'do-not-store-plaintext')
        self.env.cr.execute(
            'SELECT meta_app_secret_encrypted FROM social_hub_meta_config WHERE id = %s',
            (config.id,),
        )
        encrypted_value = self.env.cr.fetchone()[0]
        self.assertTrue(encrypted_value)
        self.assertNotIn('do-not-store-plaintext', encrypted_value)

    def test_company_record_rules_isolate_accounts(self):
        other_company = self.env['res.company'].create({'name': 'Other Social Company'})
        platform = self.env.ref('social_hub.platform_facebook')
        own_account = self.env['social.hub.account'].create({
            'name': 'Own Page',
            'handle': '@own',
            'platform_id': platform.id,
            'company_id': self.env.company.id,
        })
        self.env['social.hub.account'].create({
            'name': 'Other Page',
            'handle': '@other',
            'platform_id': platform.id,
            'company_id': other_company.id,
        })
        user = new_test_user(
            self.env,
            login='social-company-user',
            groups='base.group_user,social_hub.group_social_hub_user',
            company_id=self.env.company.id,
            company_ids=[Command.set([self.env.company.id])],
        )
        visible_accounts = self.env['social.hub.account'].with_user(user).search([])
        self.assertIn(own_account, visible_accounts)
        self.assertFalse(visible_accounts.filtered(lambda account: account.company_id == other_company))

    def test_manual_failure_is_persisted(self):
        platform = self.env.ref('social_hub.platform_facebook')
        account = self.env['social.hub.account'].create({
            'name': 'Publishing Page',
            'handle': '@publishing',
            'platform_id': platform.id,
            'company_id': self.env.company.id,
            'external_uid': 'page-123',
            'access_token': 'test-token',
            'state': 'connected',
        })
        post = self.env['social.hub.post'].create({
            'name': 'Failure Test',
            'account_id': account.id,
            'message': 'Hello',
        })
        with patch.object(type(post), '_publish_to_provider', side_effect=UserError('provider unavailable')):
            post.action_publish_now()
        self.assertEqual(post.state, 'failed')
        self.assertEqual(post.attempt_count, 1)
        self.assertIn('provider unavailable', post.last_error)

    def test_stream_refresh_stores_provider_items(self):
        platform = self.env.ref('social_hub.platform_instagram')
        account = self.env['social.hub.account'].create({
            'name': 'Instagram Account',
            'handle': '@instagram_test',
            'platform_id': platform.id,
            'company_id': self.env.company.id,
            'external_uid': 'ig-123',
            'access_token': 'test-token',
            'state': 'connected',
        })
        stream = self.env['social.hub.stream'].create({
            'name': 'Instagram Feed',
            'account_id': account.id,
            'stream_type': 'profile',
        })
        payload = {
            'data': [{
                'id': 'media-1',
                'caption': 'A real provider item',
                'media_type': 'IMAGE',
                'permalink': 'https://www.instagram.com/p/example/',
                'timestamp': '2026-07-18T01:02:03+0000',
                'username': 'instagram_test',
            }],
        }
        with patch.object(type(account), '_meta_request', return_value=payload):
            stream._refresh_stream()
        self.assertEqual(stream.last_item_count, 1)
        self.assertEqual(stream.item_ids.external_uid, 'media-1')


@tagged('post_install', '-at_install')
class TestSocialHubOAuth(HttpCase):

    def test_callback_without_state_does_not_modify_account(self):
        account = self.env['social.hub.account'].create({
            'name': 'Protected Account',
            'handle': '@protected',
            'platform_id': self.env.ref('social_hub.platform_facebook').id,
            'company_id': self.env.company.id,
            'oauth_provider': 'meta',
            'state': 'connected',
        })
        response = self.url_open(
            '/social_hub/oauth/meta/callback?error=access_denied',
            allow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        account.invalidate_recordset()
        self.assertEqual(account.state, 'connected')
