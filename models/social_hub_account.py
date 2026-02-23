import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class SocialHubAccount(models.Model):
    _name = 'social.hub.account'
    _description = 'Social Hub Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'platform_id, name'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    platform_id = fields.Many2one('social.hub.platform', required=True, ondelete='restrict', tracking=True)
    platform_code = fields.Selection(related='platform_id.code', store=True)
    handle = fields.Char(required=True, help='For example: @brand_official', tracking=True)
    external_uid = fields.Char(help='Provider-side account id/page id/channel id.')
    profile_url = fields.Char()

    access_token = fields.Char(groups='social_hub.group_social_hub_manager')
    token_expires_at = fields.Datetime(groups='social_hub.group_social_hub_manager')
    meta_user_access_token = fields.Char(groups='social_hub.group_social_hub_manager')
    meta_user_token_expires_at = fields.Datetime(groups='social_hub.group_social_hub_manager')
    meta_last_refresh_at = fields.Datetime(groups='social_hub.group_social_hub_manager')

    state = fields.Selection(
        [('draft', 'Draft'), ('connected', 'Connected'), ('disconnected', 'Disconnected')],
        default='draft',
        tracking=True,
    )
    oauth_provider = fields.Selection([
        ('meta', 'Meta'),
    ], groups='social_hub.group_social_hub_manager')
    oauth_state = fields.Char(groups='social_hub.group_social_hub_manager')
    oauth_state_expires_at = fields.Datetime(groups='social_hub.group_social_hub_manager')

    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        domain=lambda self: [('id', 'in', self.env.companies.ids)],
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

    def _compute_stream_count(self):
        for account in self:
            account.stream_count = len(account.stream_ids)

    @api.constrains('handle')
    def _check_handle(self):
        for record in self:
            if not record.handle or len(record.handle.strip()) < 2:
                raise ValidationError(_('Handle must be at least 2 characters.'))

    def action_mark_connected(self):
        self.write({'state': 'connected', 'last_sync_at': fields.Datetime.now()})

    def action_mark_disconnected(self):
        self.write({'state': 'disconnected'})

    def action_connect_meta(self):
        self.ensure_one()
        if self.platform_code not in ('facebook', 'instagram'):
            raise UserError(_('Meta OAuth is only available for Facebook and Instagram accounts.'))

        conf = self._get_meta_conf()
        app_id = conf['app_id']
        if not app_id:
            raise UserError(_('Please set Meta App ID in Social Hub settings first.'))

        state = secrets.token_urlsafe(24)
        self.write({
            'oauth_provider': 'meta',
            'oauth_state': state,
            'oauth_state_expires_at': fields.Datetime.now() + timedelta(minutes=15),
        })

        params = {
            'client_id': app_id,
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
        if self.platform_code not in ('facebook', 'instagram'):
            raise UserError(_('Meta sync is only available for Facebook and Instagram accounts.'))

        user_token = self.meta_user_access_token or self.access_token
        if not user_token:
            raise UserError(_('No token found. Please connect Meta OAuth first.'))

        self._meta_sync_from_user_access_token(user_token)

    def action_refresh_meta_token(self):
        for account in self:
            if account.platform_code not in ('facebook', 'instagram'):
                continue
            account._meta_refresh_user_access_token(force=True)
            account._meta_sync_from_user_access_token(account.meta_user_access_token or account.access_token)

    def _get_meta_conf(self):
        conf = self.env['social.hub.meta.config'].sudo().search(
            [('company_id', '=', self.company_id.id if self else self.env.company.id)],
            limit=1,
        )
        if not conf:
            return {
                'app_id': '',
                'app_secret': '',
                'version': 'v25.0',
                'scopes': 'pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,business_management',
            }
        return {
            'app_id': conf.meta_app_id or '',
            'app_secret': conf.meta_app_secret or '',
            'version': conf.meta_graph_version or 'v25.0',
            'scopes': conf.meta_scopes or 'pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,business_management',
        }

    def _meta_redirect_uri(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return f'{base_url}/social_hub/oauth/meta/callback'

    def _meta_graph_base(self):
        conf = self._get_meta_conf()
        return f"https://graph.facebook.com/{conf['version']}"

    def _meta_exchange_and_sync(self, code):
        self.ensure_one()
        conf = self._get_meta_conf()
        if not conf['app_id'] or not conf['app_secret']:
            raise UserError(_('Meta App ID / App Secret are required in settings.'))

        token_resp = requests.get(
            f"{self._meta_graph_base()}/oauth/access_token",
            params={
                'client_id': conf['app_id'],
                'client_secret': conf['app_secret'],
                'redirect_uri': self._meta_redirect_uri(),
                'code': code,
            },
            timeout=30,
        )
        token_data = token_resp.json()
        if token_resp.status_code >= 400 or token_data.get('error'):
            raise UserError(_('Meta token exchange failed: %s') % token_data)

        short_token = token_data.get('access_token')
        expires_in = int(token_data.get('expires_in') or 0)
        if not short_token:
            raise UserError(_('Meta token exchange returned no access token.'))

        self.write({
            'meta_user_access_token': short_token,
            'meta_user_token_expires_at': fields.Datetime.now() + timedelta(seconds=expires_in) if expires_in else False,
            'oauth_state': False,
            'oauth_state_expires_at': False,
        })

        self._meta_refresh_user_access_token(force=True)
        self._meta_sync_from_user_access_token(self.meta_user_access_token or short_token)

    def _meta_refresh_user_access_token(self, force=False):
        self.ensure_one()
        if self.platform_code not in ('facebook', 'instagram'):
            return False
        if not self.meta_user_access_token:
            return False

        if not force and self.meta_user_token_expires_at:
            if self.meta_user_token_expires_at > fields.Datetime.now() + timedelta(days=10):
                return False

        conf = self._get_meta_conf()
        if not conf['app_id'] or not conf['app_secret']:
            raise UserError(_('Meta App ID / App Secret are required in settings.'))

        refresh_resp = requests.get(
            f"{self._meta_graph_base()}/oauth/access_token",
            params={
                'grant_type': 'fb_exchange_token',
                'client_id': conf['app_id'],
                'client_secret': conf['app_secret'],
                'fb_exchange_token': self.meta_user_access_token,
            },
            timeout=30,
        )
        refresh_data = refresh_resp.json()
        if refresh_resp.status_code >= 400 or refresh_data.get('error'):
            raise UserError(_('Meta token refresh failed: %s') % refresh_data)

        new_token = refresh_data.get('access_token')
        expires_in = int(refresh_data.get('expires_in') or 0)
        if not new_token:
            raise UserError(_('Meta token refresh returned no access token.'))

        self.write({
            'meta_user_access_token': new_token,
            'meta_user_token_expires_at': fields.Datetime.now() + timedelta(seconds=expires_in) if expires_in else False,
            'meta_last_refresh_at': fields.Datetime.now(),
        })
        return True

    def _meta_sync_from_user_access_token(self, user_access_token):
        self.ensure_one()
        graph_base = self._meta_graph_base()

        me_resp = requests.get(
            f"{graph_base}/me",
            params={'fields': 'id,name', 'access_token': user_access_token},
            timeout=30,
        )
        me_data = me_resp.json()
        if me_resp.status_code >= 400 or me_data.get('error'):
            raise UserError(_('Meta /me failed: %s') % me_data)

        pages_resp = requests.get(
            f"{graph_base}/me/accounts",
            params={
                'fields': 'id,name,access_token,link,instagram_business_account{id,username,name,profile_picture_url}',
                'access_token': user_access_token,
            },
            timeout=30,
        )
        pages_data = pages_resp.json()
        if pages_resp.status_code >= 400 or pages_data.get('error'):
            raise UserError(_('Meta /me/accounts failed: %s') % pages_data)

        pages = pages_data.get('data') or []
        if not pages:
            raise UserError(_('No Facebook Pages available for this user token.'))

        if self.platform_code == 'facebook':
            page = pages[0]
            page_name = page.get('name') or self.name
            page_token = page.get('access_token') or user_access_token
            page_id = page.get('id')
            page_link = page.get('link') or (f'https://www.facebook.com/{page_id}' if page_id else False)
            self.write({
                'name': page_name,
                'handle': self.handle if self.handle else page_name,
                'external_uid': page_id,
                'profile_url': page_link,
                'access_token': page_token,
                'state': 'connected',
                'last_sync_at': fields.Datetime.now(),
            })
            return

        ig_target = False
        source_page = False
        for page in pages:
            ig = page.get('instagram_business_account')
            if ig:
                ig_target = ig
                source_page = page
                break

        if not ig_target:
            raise UserError(_('No Instagram Business account found in accessible Facebook Pages.'))

        ig_id = ig_target.get('id')
        ig_username = ig_target.get('username')
        ig_name = ig_target.get('name') or ig_username or self.name
        ig_picture = ig_target.get('profile_picture_url')

        if ig_id and (not ig_username or not ig_name):
            ig_resp = requests.get(
                f"{graph_base}/{ig_id}",
                params={
                    'fields': 'id,username,name,profile_picture_url',
                    'access_token': source_page.get('access_token') or user_access_token,
                },
                timeout=30,
            )
            ig_data = ig_resp.json()
            if ig_resp.status_code < 400 and not ig_data.get('error'):
                ig_username = ig_data.get('username') or ig_username
                ig_name = ig_data.get('name') or ig_name
                ig_picture = ig_data.get('profile_picture_url') or ig_picture

        self.write({
            'name': ig_name or self.name,
            'handle': ig_username or self.handle,
            'external_uid': ig_id,
            'profile_url': f'https://www.instagram.com/{ig_username}/' if ig_username else ig_picture,
            'access_token': source_page.get('access_token') or user_access_token,
            'state': 'connected',
            'last_sync_at': fields.Datetime.now(),
        })

    @api.model
    def cron_refresh_meta_tokens(self):
        accounts = self.sudo().search([
            ('platform_code', 'in', ['facebook', 'instagram']),
            ('state', '=', 'connected'),
            ('meta_user_access_token', '!=', False),
        ])
        for account in accounts:
            try:
                refreshed = account._meta_refresh_user_access_token(force=False)
                if refreshed:
                    account._meta_sync_from_user_access_token(account.meta_user_access_token)
                    account.message_post(body='Meta token refreshed automatically.')
            except Exception as exc:
                account.message_post(body=f'Automatic Meta token refresh failed: {exc}')
