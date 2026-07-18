from odoo import fields, models


class SocialHubStreamItem(models.Model):
    _name = 'social.hub.stream.item'
    _description = 'Social Hub Stream Item'
    _order = 'published_at desc, id desc'
    _check_company_auto = True

    stream_id = fields.Many2one(
        'social.hub.stream',
        required=True,
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    account_id = fields.Many2one(related='stream_id.account_id', store=True, readonly=True)
    company_id = fields.Many2one(
        related='stream_id.company_id',
        store=True,
        readonly=True,
        index=True,
    )
    platform_id = fields.Many2one(related='stream_id.platform_id', store=True, readonly=True)
    external_uid = fields.Char(required=True, index=True)
    item_type = fields.Selection(
        [('post', 'Post'), ('image', 'Image'), ('video', 'Video'), ('reel', 'Reel')],
        default='post',
        required=True,
    )
    author_name = fields.Char()
    message = fields.Text()
    published_at = fields.Datetime(index=True)
    permalink = fields.Char()
    media_url = fields.Char()
    raw_payload = fields.Text(groups='social_hub.group_social_hub_manager')

    _stream_external_uid_unique = models.Constraint(
        'UNIQUE(stream_id, external_uid)',
        'This provider item already exists in the stream.',
    )

