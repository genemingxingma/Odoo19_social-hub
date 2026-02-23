from datetime import timedelta

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SocialHubPost(models.Model):
    _name = 'social.hub.post'
    _description = 'Social Hub Post'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)

    account_id = fields.Many2one('social.hub.account', required=True, ondelete='restrict', tracking=True)
    platform_id = fields.Many2one(related='account_id.platform_id', store=True, readonly=True)
    platform_code = fields.Selection(related='account_id.platform_code', store=True, readonly=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True, readonly=True)

    media_type = fields.Selection(
        [('text', 'Text'), ('image', 'Image'), ('video', 'Video')],
        default='text',
        required=True,
        tracking=True,
    )
    message = fields.Text(required=True)
    image_url = fields.Char(help='Image URL for image posts.')
    video_url = fields.Char(help='Video URL for video posts.')

    scheduled_at = fields.Datetime(help='If set in the future, publish job will wait until this time.')
    state = fields.Selection(
        [('draft', 'Draft'), ('queued', 'Queued'), ('processing', 'Processing'), ('posted', 'Posted'), ('failed', 'Failed'), ('canceled', 'Canceled')],
        default='draft',
        tracking=True,
    )

    attempt_count = fields.Integer(default=0, readonly=True)
    max_attempts = fields.Integer(default=3)
    retry_interval_minutes = fields.Integer(default=10)
    next_retry_at = fields.Datetime(readonly=True)

    external_post_id = fields.Char(readonly=True)
    external_permalink = fields.Char(readonly=True)
    posted_at = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    provider_response = fields.Text(readonly=True)

    def action_publish_now(self):
        for post in self:
            post._attempt_publish(manual=True)

    def action_queue_publish(self):
        now = fields.Datetime.now()
        for post in self:
            post.write({
                'state': 'queued',
                'next_retry_at': post.scheduled_at or now,
                'last_error': False,
            })

    def action_cancel(self):
        self.write({'state': 'canceled'})

    def action_reset_draft(self):
        self.write({
            'state': 'draft',
            'attempt_count': 0,
            'next_retry_at': False,
            'last_error': False,
            'provider_response': False,
        })

    def _attempt_publish(self, manual=False):
        self.ensure_one()

        if self.state == 'canceled':
            return

        if self.scheduled_at and self.scheduled_at > fields.Datetime.now() and not manual:
            return

        self.write({'state': 'processing'})
        try:
            result = self._publish_to_provider()
            self.write({
                'state': 'posted',
                'external_post_id': result.get('id') or result.get('post_id') or result.get('creation_id'),
                'external_permalink': result.get('permalink_url') or False,
                'posted_at': fields.Datetime.now(),
                'last_error': False,
                'provider_response': str(result),
            })
            self.message_post(body=_('Post published successfully: %s') % (self.external_post_id or 'ok'))
        except Exception as exc:
            attempts = (self.attempt_count or 0) + 1
            will_retry = attempts < (self.max_attempts or 1)
            vals = {
                'attempt_count': attempts,
                'last_error': str(exc),
                'provider_response': str(exc),
                'state': 'queued' if will_retry and not manual else 'failed',
                'next_retry_at': fields.Datetime.now() + timedelta(minutes=max(1, self.retry_interval_minutes or 10)) if will_retry and not manual else False,
            }
            self.write(vals)
            self.message_post(body=_('Publish failed (attempt %s/%s): %s') % (attempts, self.max_attempts, str(exc)))
            if manual:
                raise

    def _publish_to_provider(self):
        self.ensure_one()
        if self.platform_code not in ('facebook', 'instagram'):
            raise UserError(_('Publishing currently supports Facebook and Instagram only.'))
        if not self.account_id.access_token:
            raise UserError(_('Account has no access token. Connect OAuth first.'))
        if self.account_id.state != 'connected':
            raise UserError(_('Account is not connected.'))

        if self.platform_code == 'facebook':
            return self._publish_facebook_page_post()
        return self._publish_instagram_post()

    def _meta_graph_base(self):
        return self.account_id._meta_graph_base()

    def _publish_facebook_page_post(self):
        if not self.account_id.external_uid:
            raise UserError(_('Facebook account has no external page id.'))

        graph_base = self._meta_graph_base()
        page_id = self.account_id.external_uid
        token = self.account_id.access_token

        if self.media_type == 'video':
            if not self.video_url:
                raise UserError(_('Facebook video post requires video_url.'))
            resp = requests.post(
                f"{graph_base}/{page_id}/videos",
                data={
                    'file_url': self.video_url,
                    'description': self.message,
                    'access_token': token,
                },
                timeout=60,
            )
            data = resp.json()
            if resp.status_code >= 400 or data.get('error'):
                raise UserError(_('Facebook video publish failed: %s') % data)
            return {'id': data.get('id')}

        payload = {
            'message': self.message,
            'access_token': token,
        }
        if self.media_type == 'image':
            if not self.image_url:
                raise UserError(_('Facebook image post requires image_url.'))
            payload['link'] = self.image_url

        resp = requests.post(f"{graph_base}/{page_id}/feed", data=payload, timeout=45)
        data = resp.json()
        if resp.status_code >= 400 or data.get('error'):
            raise UserError(_('Facebook publish failed: %s') % data)

        post_id = data.get('id')
        permalink = False
        if post_id:
            p_resp = requests.get(
                f"{graph_base}/{post_id}",
                params={'fields': 'id,permalink_url', 'access_token': token},
                timeout=30,
            )
            p_data = p_resp.json()
            if p_resp.status_code < 400 and not p_data.get('error'):
                permalink = p_data.get('permalink_url')

        return {'id': post_id, 'permalink_url': permalink}

    def _publish_instagram_post(self):
        if not self.account_id.external_uid:
            raise UserError(_('Instagram account has no external IG user id.'))

        graph_base = self._meta_graph_base()
        ig_user_id = self.account_id.external_uid
        token = self.account_id.access_token

        if self.media_type == 'text':
            raise UserError(_('Instagram does not support text-only publishing in this flow. Use image or video.'))

        create_payload = {
            'caption': self.message,
            'access_token': token,
        }
        if self.media_type == 'image':
            if not self.image_url:
                raise UserError(_('Instagram image post requires image_url.'))
            create_payload['image_url'] = self.image_url
        else:
            if not self.video_url:
                raise UserError(_('Instagram video post requires video_url.'))
            create_payload['video_url'] = self.video_url
            create_payload['media_type'] = 'REELS'

        create_resp = requests.post(f"{graph_base}/{ig_user_id}/media", data=create_payload, timeout=60)
        create_data = create_resp.json()
        if create_resp.status_code >= 400 or create_data.get('error'):
            raise UserError(_('Instagram media container creation failed: %s') % create_data)

        creation_id = create_data.get('id')
        if not creation_id:
            raise UserError(_('Instagram media container id missing.'))

        publish_resp = requests.post(
            f"{graph_base}/{ig_user_id}/media_publish",
            data={'creation_id': creation_id, 'access_token': token},
            timeout=45,
        )
        publish_data = publish_resp.json()
        if publish_resp.status_code >= 400 or publish_data.get('error'):
            raise UserError(_('Instagram media publish failed: %s') % publish_data)

        ig_media_id = publish_data.get('id')
        permalink = False
        if ig_media_id:
            detail_resp = requests.get(
                f"{graph_base}/{ig_media_id}",
                params={'fields': 'id,permalink', 'access_token': token},
                timeout=30,
            )
            detail_data = detail_resp.json()
            if detail_resp.status_code < 400 and not detail_data.get('error'):
                permalink = detail_data.get('permalink')

        return {'id': ig_media_id, 'creation_id': creation_id, 'permalink_url': permalink}

    @api.model
    def cron_process_publish_queue(self):
        now = fields.Datetime.now()
        domain = [
            ('state', 'in', ['queued', 'failed']),
            '|', ('next_retry_at', '=', False), ('next_retry_at', '<=', now),
            '|', ('scheduled_at', '=', False), ('scheduled_at', '<=', now),
        ]
        posts = self.sudo().search(domain, limit=50)
        for post in posts:
            if post.attempt_count >= max(1, post.max_attempts or 1):
                continue
            post._attempt_publish(manual=False)
