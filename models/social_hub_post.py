import json
import logging
import time
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError, ValidationError


_logger = logging.getLogger(__name__)


class SocialHubPost(models.Model):
    _name = 'social.hub.post'
    _description = 'Social Hub Post'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)

    account_id = fields.Many2one(
        'social.hub.account',
        required=True,
        ondelete='restrict',
        tracking=True,
        check_company=True,
    )
    platform_id = fields.Many2one(related='account_id.platform_id', store=True, readonly=True)
    platform_code = fields.Selection(related='account_id.platform_code', store=True, readonly=True)
    company_id = fields.Many2one(
        related='account_id.company_id',
        store=True,
        readonly=True,
        index=True,
    )

    media_type = fields.Selection(
        [('text', 'Text'), ('image', 'Image'), ('video', 'Video')],
        default='text',
        required=True,
        tracking=True,
    )
    message = fields.Text(required=True)
    image_url = fields.Char(help='Public HTTPS image URL for image posts.')
    video_url = fields.Char(help='Public HTTPS video URL for video posts.')

    scheduled_at = fields.Datetime(
        help='If set in the future, the publish job waits until this time.',
        index=True,
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('queued', 'Queued'),
            ('processing', 'Processing'),
            ('posted', 'Posted'),
            ('failed', 'Failed'),
            ('canceled', 'Canceled'),
        ],
        default='draft',
        tracking=True,
        index=True,
    )

    attempt_count = fields.Integer(default=0, readonly=True)
    max_attempts = fields.Integer(default=3)
    retry_interval_minutes = fields.Integer(default=10)
    next_retry_at = fields.Datetime(readonly=True, index=True)
    processing_started_at = fields.Datetime(readonly=True, index=True)
    publish_run_uuid = fields.Char(readonly=True, copy=False, index=True)

    external_post_id = fields.Char(readonly=True, copy=False)
    external_permalink = fields.Char(readonly=True, copy=False)
    posted_at = fields.Datetime(readonly=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)
    provider_response = fields.Text(readonly=True, copy=False)

    @api.constrains('max_attempts', 'retry_interval_minutes')
    def _check_retry_settings(self):
        for post in self:
            if post.max_attempts < 1:
                raise ValidationError(_('Maximum attempts must be at least 1.'))
            if post.retry_interval_minutes < 1:
                raise ValidationError(_('Retry interval must be at least 1 minute.'))

    @api.constrains('message', 'account_id')
    def _check_message_length(self):
        for post in self:
            maximum = post.platform_id.max_post_length
            if maximum and len(post.message or '') > maximum:
                raise ValidationError(_(
                    'The message exceeds the %(platform)s limit of %(limit)s characters.',
                    platform=post.platform_id.name,
                    limit=maximum,
                ))

    def action_publish_now(self):
        successes = 0
        failures = []
        for post in self:
            success, message = post._attempt_publish(manual=True, commit_claim=True)
            if success:
                successes += 1
            elif message:
                failures.append(f'{post.display_name}: {message}')
        if failures:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Publishing completed with errors'),
                    'message': '\n'.join(failures),
                    'type': 'warning',
                    'sticky': True,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Published'),
                'message': _('%s post(s) published successfully.') % successes,
                'type': 'success',
                'sticky': False,
            },
        }

    def action_queue_publish(self):
        now = fields.Datetime.now()
        for post in self:
            if post.state in ('posted', 'processing'):
                raise UserError(_('Posted or processing records cannot be queued again.'))
            post._validate_publish_payload()
            post.write({
                'state': 'queued',
                'next_retry_at': post.scheduled_at or now,
                'last_error': False,
                'processing_started_at': False,
                'publish_run_uuid': False,
            })

    def action_cancel(self):
        for post in self:
            if post.state in ('posted', 'processing'):
                raise UserError(_('A posted or processing record cannot be canceled.'))
        self.write({'state': 'canceled', 'next_retry_at': False})

    def action_reset_draft(self):
        for post in self:
            if post.state in ('posted', 'processing'):
                raise UserError(_('A posted or processing record cannot be reset to draft.'))
        self.write({
            'state': 'draft',
            'attempt_count': 0,
            'next_retry_at': False,
            'processing_started_at': False,
            'publish_run_uuid': False,
            'last_error': False,
            'provider_response': False,
        })

    def _validate_public_url(self, value, label):
        parsed = urlparse(value or '')
        if parsed.scheme != 'https' or not parsed.netloc:
            raise UserError(_('%s must be a publicly reachable HTTPS URL.') % label)

    def _validate_publish_payload(self):
        self.ensure_one()
        self.account_id.check_access('read')
        if not self.platform_id.supports_posting:
            raise UserError(_('%s is not enabled for publishing.') % self.platform_id.name)
        if self.platform_code not in ('facebook', 'instagram'):
            raise UserError(_('Publishing currently supports Facebook and Instagram only.'))
        if self.account_id.state != 'connected':
            raise UserError(_('Account is not connected.'))
        if not self.account_id.external_uid:
            raise UserError(_('Account has no platform asset ID. Sync Meta assets first.'))
        if not self.account_id.sudo().access_token:
            raise UserError(_('Account has no access token. Connect OAuth first.'))
        if self.media_type == 'image':
            self._validate_public_url(self.image_url, _('Image URL'))
        elif self.media_type == 'video':
            self._validate_public_url(self.video_url, _('Video URL'))
        if self.platform_code == 'instagram' and self.media_type == 'text':
            raise UserError(_('Instagram does not support text-only publishing.'))

    def _attempt_publish(self, manual=False, commit_claim=False):
        self.ensure_one()
        locked = self.try_lock_for_update(limit=1)
        if not locked:
            return False, _('Another worker is already processing this post.') if manual else False
        post = locked

        if post.state in ('posted', 'processing', 'canceled'):
            return False, _('The post is already posted, processing, or canceled.')
        if post.scheduled_at and post.scheduled_at > fields.Datetime.now() and not manual:
            return False, False

        try:
            post._validate_publish_payload()
        except Exception as exc:
            message = post._safe_error_message(exc)
            post._record_publish_failure(message, manual=manual)
            return False, message

        post.write({
            'state': 'processing',
            'processing_started_at': fields.Datetime.now(),
            'publish_run_uuid': str(uuid.uuid4()),
            'last_error': False,
        })
        if commit_claim and not tools.config.get('test_enable'):
            self.env['ir.cron']._commit_progress()

        try:
            result = post._publish_to_provider()
            serialized_result = json.dumps(result, ensure_ascii=False, default=str)[:20000]
            post.write({
                'state': 'posted',
                'external_post_id': result.get('id') or result.get('post_id') or result.get('creation_id'),
                'external_permalink': result.get('permalink_url') or False,
                'posted_at': fields.Datetime.now(),
                'processing_started_at': False,
                'next_retry_at': False,
                'last_error': False,
                'provider_response': serialized_result,
            })
            post.message_post(body=_('Post published successfully: %s') % (post.external_post_id or 'ok'))
            return True, False
        except Exception as exc:
            _logger.exception('Social Hub publishing failed for post %s.', post.id)
            message = post._safe_error_message(exc)
            post._record_publish_failure(message, manual=manual)
            return False, message

    def _safe_error_message(self, exc):
        message = str(exc)[:2000] or exc.__class__.__name__
        account = self.account_id.sudo()
        for secret in (account.access_token, account.meta_user_access_token):
            if secret:
                message = message.replace(secret, '***')
        return message

    def _record_publish_failure(self, message, manual=False):
        self.ensure_one()
        attempts = (self.attempt_count or 0) + 1
        will_retry = attempts < self.max_attempts and not manual
        self.write({
            'attempt_count': attempts,
            'last_error': message,
            'provider_response': False,
            'state': 'queued' if will_retry else 'failed',
            'next_retry_at': (
                fields.Datetime.now() + timedelta(minutes=self.retry_interval_minutes)
                if will_retry else False
            ),
            'processing_started_at': False,
        })
        self.message_post(body=_('Publish failed (attempt %s/%s): %s') % (
            attempts,
            self.max_attempts,
            message,
        ))

    def _publish_to_provider(self):
        self.ensure_one()
        if self.platform_code == 'facebook':
            return self._publish_facebook_page_post()
        if self.platform_code == 'instagram':
            return self._publish_instagram_post()
        raise UserError(_('Publishing currently supports Facebook and Instagram only.'))

    def _publish_facebook_page_post(self):
        account = self.account_id.sudo()
        page_id = account.external_uid
        token = account.access_token

        if self.media_type == 'video':
            result = account._meta_request(
                'POST',
                f'{page_id}/videos',
                data={'file_url': self.video_url, 'description': self.message},
                token=token,
                timeout=90,
            )
            return {'id': result.get('id')}

        if self.media_type == 'image':
            result = account._meta_request(
                'POST',
                f'{page_id}/photos',
                data={'url': self.image_url, 'caption': self.message, 'published': 'true'},
                token=token,
                timeout=90,
            )
            post_id = result.get('post_id') or result.get('id')
        else:
            result = account._meta_request(
                'POST',
                f'{page_id}/feed',
                data={'message': self.message},
                token=token,
                timeout=45,
            )
            post_id = result.get('id')

        permalink = False
        if post_id:
            details = account._meta_request(
                'GET',
                post_id,
                params={'fields': 'id,permalink_url'},
                token=token,
            )
            permalink = details.get('permalink_url')
        return {'id': post_id, 'permalink_url': permalink}

    def _publish_instagram_post(self):
        account = self.account_id.sudo()
        ig_user_id = account.external_uid
        token = account.access_token
        create_payload = {'caption': self.message}
        if self.media_type == 'image':
            create_payload['image_url'] = self.image_url
        else:
            create_payload.update({'video_url': self.video_url, 'media_type': 'REELS'})

        container = account._meta_request(
            'POST',
            f'{ig_user_id}/media',
            data=create_payload,
            token=token,
            timeout=90,
        )
        creation_id = container.get('id')
        if not creation_id:
            raise UserError(_('Instagram media container ID is missing.'))

        self._wait_for_instagram_container(account, creation_id, token)
        published = account._meta_request(
            'POST',
            f'{ig_user_id}/media_publish',
            data={'creation_id': creation_id},
            token=token,
            timeout=60,
        )
        media_id = published.get('id')
        permalink = False
        if media_id:
            details = account._meta_request(
                'GET',
                media_id,
                params={'fields': 'id,permalink'},
                token=token,
            )
            permalink = details.get('permalink')
        return {'id': media_id, 'creation_id': creation_id, 'permalink_url': permalink}

    def _wait_for_instagram_container(self, account, creation_id, token):
        timeout_seconds = 120 if self.media_type == 'video' else 45
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = account._meta_request(
                'GET',
                creation_id,
                params={'fields': 'status_code,status'},
                token=token,
            )
            status_code = status.get('status_code')
            if status_code == 'FINISHED':
                return
            if status_code in ('ERROR', 'EXPIRED'):
                raise UserError(_('Instagram media processing failed: %s') % (
                    status.get('status') or status_code,
                ))
            time.sleep(3)
        raise UserError(_('Instagram media processing timed out before publishing.'))

    @api.model
    def cron_process_publish_queue(self):
        now = fields.Datetime.now()
        stale_before = now - timedelta(minutes=30)
        stale_posts = self.sudo().search([
            ('state', '=', 'processing'),
            ('processing_started_at', '<=', stale_before),
        ])
        if stale_posts:
            stale_posts.write({
                'state': 'failed',
                'processing_started_at': False,
                'next_retry_at': False,
                'last_error': _(
                    'Publishing stopped unexpectedly. The provider outcome is unknown; review before retrying.'
                ),
            })

        domain = [
            ('state', '=', 'queued'),
            '|', ('next_retry_at', '=', False), ('next_retry_at', '<=', now),
            '|', ('scheduled_at', '=', False), ('scheduled_at', '<=', now),
        ]
        remaining = self.sudo().search_count(domain)
        self.env['ir.cron']._commit_progress(remaining=remaining)
        processed = 0
        while processed < 20:
            post = self.sudo().search(domain, order='next_retry_at, id', limit=1)
            if not post:
                break
            success, message = post._attempt_publish(manual=False, commit_claim=True)
            if not success and not message:
                break
            processed += 1
            remaining = self.sudo().search_count(domain)
            if not self.env['ir.cron']._commit_progress(processed=1, remaining=remaining):
                break
