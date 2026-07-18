import json
import logging
from datetime import timezone

from dateutil.parser import isoparse

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


_logger = logging.getLogger(__name__)


def _provider_datetime(value):
    if not value:
        return False
    parsed = isoparse(value)
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class SocialHubStream(models.Model):
    _name = 'social.hub.stream'
    _description = 'Social Hub Stream'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    account_id = fields.Many2one(
        'social.hub.account',
        required=True,
        ondelete='cascade',
        tracking=True,
        check_company=True,
    )
    platform_id = fields.Many2one(
        'social.hub.platform',
        related='account_id.platform_id',
        store=True,
        readonly=True,
    )
    platform_code = fields.Selection(related='account_id.platform_code', store=True, readonly=True)
    company_id = fields.Many2one(
        'res.company',
        related='account_id.company_id',
        store=True,
        readonly=True,
        index=True,
    )

    stream_type = fields.Selection(
        [
            ('profile', 'Profile Feed'),
            ('hashtag', 'Profile Hashtag Filter'),
            ('keyword', 'Profile Keyword Filter'),
            ('mention', 'Profile Mention Filter'),
        ],
        required=True,
        default='profile',
        tracking=True,
    )
    query = fields.Char(help='Optional local filter applied to the connected account feed.')
    source_url = fields.Char(readonly=True)

    last_fetch_at = fields.Datetime(readonly=True)
    last_success_at = fields.Datetime(readonly=True)
    last_item_count = fields.Integer(readonly=True, default=0)
    last_error = fields.Text(readonly=True)
    note = fields.Text()
    item_ids = fields.One2many('social.hub.stream.item', 'stream_id')

    @api.constrains('stream_type', 'query')
    def _check_query(self):
        for stream in self:
            if stream.stream_type != 'profile' and not (stream.query or '').strip():
                raise ValidationError(_('A query is required for filtered streams.'))

    def _check_social_hub_manager(self):
        if not self.env.user.has_group('social_hub.group_social_hub_manager'):
            raise AccessError(_('Only Social Hub managers can refresh streams.'))

    def action_refresh_stream(self):
        self._check_social_hub_manager()
        failures = []
        for stream in self:
            try:
                stream._refresh_stream()
            except Exception as exc:
                _logger.exception('Social Hub stream refresh failed for stream %s.', stream.id)
                failures.append(f'{stream.display_name}: {str(exc)[:500]}')
        if failures:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Stream refresh completed with errors'),
                    'message': '\n'.join(failures),
                    'type': 'warning',
                    'sticky': True,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Streams refreshed'),
                'message': _('%s stream(s) refreshed.') % len(self),
                'type': 'success',
                'sticky': False,
            },
        }

    def _refresh_stream(self):
        self.ensure_one()
        self.check_access('write')
        account = self.account_id
        if account.state != 'connected':
            raise UserError(_('The stream account is not connected.'))
        if not self.platform_id.supports_streams or self.platform_code not in ('facebook', 'instagram'):
            raise UserError(_('Live stream synchronization currently supports Facebook and Instagram only.'))

        try:
            items = self._fetch_meta_items()
            self._upsert_items(items)
            now = fields.Datetime.now()
            item_count = self.env['social.hub.stream.item'].search_count([('stream_id', '=', self.id)])
            self.write({
                'last_fetch_at': now,
                'last_success_at': now,
                'last_item_count': item_count,
                'last_error': False,
            })
        except Exception as exc:
            message = str(exc)[:2000]
            self.write({'last_fetch_at': fields.Datetime.now(), 'last_error': message})
            raise

    def _fetch_meta_items(self):
        self.ensure_one()
        account = self.account_id.sudo()
        token = account.access_token
        if not token:
            raise UserError(_('The connected account has no access token.'))

        if self.platform_code == 'facebook':
            payload = account._meta_request(
                'GET',
                f'{account.external_uid}/feed',
                params={
                    'fields': 'id,message,created_time,permalink_url,from,attachments{media,type,url}',
                    'limit': 50,
                },
                token=token,
            )
            items = [self._normalize_facebook_item(item) for item in payload.get('data') or []]
            self.source_url = account.profile_url
        else:
            payload = account._meta_request(
                'GET',
                f'{account.external_uid}/media',
                params={
                    'fields': 'id,caption,media_type,media_url,permalink,timestamp,username',
                    'limit': 50,
                },
                token=token,
            )
            items = [self._normalize_instagram_item(item) for item in payload.get('data') or []]
            self.source_url = account.profile_url
        return [item for item in items if item and self._matches_query(item.get('message'))]

    def _matches_query(self, message):
        if self.stream_type == 'profile':
            return True
        text = (message or '').casefold()
        query = (self.query or '').strip().casefold()
        if self.stream_type == 'hashtag':
            query = query if query.startswith('#') else f'#{query}'
        elif self.stream_type == 'mention':
            query = query if query.startswith('@') else f'@{query}'
        return query in text

    def _normalize_facebook_item(self, item):
        attachment = ((item.get('attachments') or {}).get('data') or [{}])[0]
        attachment_type = (attachment.get('type') or '').lower()
        item_type = 'video' if 'video' in attachment_type else 'image' if 'photo' in attachment_type else 'post'
        media = attachment.get('media') or {}
        image = media.get('image') or {}
        return {
            'external_uid': item.get('id'),
            'item_type': item_type,
            'author_name': (item.get('from') or {}).get('name'),
            'message': item.get('message'),
            'published_at': _provider_datetime(item.get('created_time')),
            'permalink': item.get('permalink_url') or attachment.get('url'),
            'media_url': image.get('src'),
            'raw_payload': json.dumps(item, ensure_ascii=False, default=str)[:50000],
        }

    def _normalize_instagram_item(self, item):
        media_type = (item.get('media_type') or '').upper()
        item_type = 'reel' if media_type == 'REELS' else 'video' if media_type == 'VIDEO' else 'image'
        return {
            'external_uid': item.get('id'),
            'item_type': item_type,
            'author_name': item.get('username'),
            'message': item.get('caption'),
            'published_at': _provider_datetime(item.get('timestamp')),
            'permalink': item.get('permalink'),
            'media_url': item.get('media_url'),
            'raw_payload': json.dumps(item, ensure_ascii=False, default=str)[:50000],
        }

    def _upsert_items(self, items):
        self.ensure_one()
        Item = self.env['social.hub.stream.item'].sudo()
        valid_items = [item for item in items if item.get('external_uid')]
        external_ids = [item['external_uid'] for item in valid_items]
        existing = Item.search([
            ('stream_id', '=', self.id),
            ('external_uid', 'in', external_ids),
        ]) if external_ids else Item
        by_external_id = {item.external_uid: item for item in existing}
        for values in valid_items:
            record = by_external_id.get(values['external_uid'])
            if record:
                record.write(values)
            else:
                Item.create({'stream_id': self.id, **values})

    @api.model
    def cron_refresh_streams(self):
        domain = [
            ('active', '=', True),
            ('account_id.state', '=', 'connected'),
            ('platform_code', 'in', ['facebook', 'instagram']),
        ]
        streams = self.sudo().search(domain, order='last_fetch_at, id', limit=20)
        self.env['ir.cron']._commit_progress(remaining=len(streams))
        for stream in streams:
            try:
                stream._refresh_stream()
            except Exception:
                _logger.exception('Social Hub stream refresh failed for stream %s.', stream.id)
                stream.message_post(body=_('Stream refresh failed. Check the server log.'))
            if not self.env['ir.cron']._commit_progress(processed=1):
                break
