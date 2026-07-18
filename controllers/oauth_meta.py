import logging

from odoo import fields, http
from odoo.http import request


_logger = logging.getLogger(__name__)


class SocialHubMetaOAuthController(http.Controller):

    @http.route(
        '/social_hub/oauth/meta/callback',
        type='http',
        auth='public',
        methods=['GET'],
        csrf=False,
        sitemap=False,
    )
    def social_hub_meta_callback(self, **kwargs):
        state = kwargs.get('state')
        code = kwargs.get('code')
        error = kwargs.get('error')
        error_reason = kwargs.get('error_reason')

        if not state or len(state) > 512:
            return request.redirect('/web?error=social_hub_oauth_state_missing')

        account = request.env['social.hub.account'].sudo().search([
            ('oauth_state', '=', state),
            ('oauth_provider', '=', 'meta'),
            ('oauth_state_expires_at', '>=', fields.Datetime.now()),
        ], limit=1)

        if not account:
            return request.redirect('/web?error=social_hub_oauth_state_not_found')

        # Consume the state atomically before any external request so replayed callbacks fail.
        request.env.cr.execute(
            """
            UPDATE social_hub_account
               SET oauth_state = NULL,
                   oauth_state_expires_at = NULL
             WHERE id = %s
               AND oauth_state = %s
            RETURNING id
            """,
            (account.id, state),
        )
        if not request.env.cr.fetchone():
            return request.redirect('/web?error=social_hub_oauth_state_replayed')
        account.invalidate_recordset(['oauth_state', 'oauth_state_expires_at'])

        if error:
            account.message_post(
                body='Meta OAuth authorization was not completed: %s%s' % (
                    str(error)[:100],
                    f' ({str(error_reason)[:100]})' if error_reason else '',
                )
            )
            return request.redirect(f'/web#id={account.id}&model=social.hub.account&view_type=form')

        if not code:
            account.message_post(body='Meta OAuth callback has no authorization code.')
            return request.redirect(f'/web#id={account.id}&model=social.hub.account&view_type=form')

        try:
            with request.env.cr.savepoint():
                account._meta_exchange_and_sync(code)
            account.message_post(body='Meta OAuth connected successfully.')
        except Exception:
            _logger.exception('Meta OAuth sync failed for Social Hub account %s.', account.id)
            account.invalidate_recordset()
            if not account.access_token_encrypted:
                account.write({'state': 'disconnected'})
            account.message_post(body='Meta OAuth sync failed. Check the server log and try again.')

        return request.redirect(f'/web#id={account.id}&model=social.hub.account&view_type=form')
