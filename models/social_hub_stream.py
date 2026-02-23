from odoo import fields, models


class SocialHubStream(models.Model):
    _name = 'social.hub.stream'
    _description = 'Social Hub Stream'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    account_id = fields.Many2one('social.hub.account', required=True, ondelete='cascade', tracking=True)
    platform_id = fields.Many2one('social.hub.platform', related='account_id.platform_id', store=True, readonly=True)
    company_id = fields.Many2one('res.company', related='account_id.company_id', store=True, readonly=True)

    stream_type = fields.Selection(
        [
            ('profile', 'Profile Feed'),
            ('hashtag', 'Hashtag'),
            ('keyword', 'Keyword'),
            ('mention', 'Mentions'),
            ('custom', 'Custom Source'),
        ],
        required=True,
        default='profile',
        tracking=True,
    )
    query = fields.Char(help='Hashtag, keyword or query expression.')
    source_url = fields.Char(help='Optional URL of the stream source.')

    last_fetch_at = fields.Datetime(readonly=True)
    last_item_count = fields.Integer(readonly=True, default=0)
    note = fields.Text()

    def action_refresh_stream(self):
        now = fields.Datetime.now()
        for stream in self:
            stream.write({
                'last_fetch_at': now,
                'last_item_count': stream.last_item_count + 1,
            })
