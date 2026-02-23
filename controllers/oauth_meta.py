from odoo import fields, http
from odoo.http import request


class SocialHubMetaOAuthController(http.Controller):

    @http.route('/social_hub/oauth/meta/callback', type='http', auth='public', methods=['GET'], csrf=False)
    def social_hub_meta_callback(self, **kwargs):
        state = kwargs.get('state')
        code = kwargs.get('code')
        error = kwargs.get('error')
        error_reason = kwargs.get('error_reason')
        error_description = kwargs.get('error_description')

        account = request.env['social.hub.account'].sudo().search([
            ('oauth_state', '=', state),
            ('oauth_provider', '=', 'meta'),
        ], limit=1)

        if not account:
            return request.redirect('/web?error=social_hub_oauth_state_not_found')
        if account.oauth_state_expires_at and account.oauth_state_expires_at < fields.Datetime.now():
            account.message_post(body='Meta OAuth callback rejected: state expired.')
            account.write({'state': 'disconnected', 'oauth_state': False, 'oauth_state_expires_at': False})
            return request.redirect(f'/web#id={account.id}&model=social.hub.account&view_type=form')

        if error:
            account.message_post(body=f'Meta OAuth failed: {error} / {error_reason or ""} / {error_description or ""}')
            account.write({'state': 'disconnected'})
            return request.redirect(f'/web#id={account.id}&model=social.hub.account&view_type=form')

        if not code:
            account.message_post(body='Meta OAuth callback has no authorization code.')
            account.write({'state': 'disconnected'})
            return request.redirect(f'/web#id={account.id}&model=social.hub.account&view_type=form')

        try:
            account._meta_exchange_and_sync(code)
            account.message_post(body='Meta OAuth connected successfully.')
        except Exception as exc:
            account.message_post(body=f'Meta OAuth sync failed: {exc}')
            account.write({'state': 'disconnected'})

        return request.redirect(f'/web#id={account.id}&model=social.hub.account&view_type=form')
