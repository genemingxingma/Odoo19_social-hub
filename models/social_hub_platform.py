from odoo import fields, models


class SocialHubPlatform(models.Model):
    _name = 'social.hub.platform'
    _description = 'Social Hub Platform'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, name'

    name = fields.Char(required=True, tracking=True)
    code = fields.Selection(
        selection=[
            ('facebook', 'Facebook'),
            ('xiaohongshu', 'Xiaohongshu'),
            ('tiktok', 'TikTok'),
            ('instagram', 'Instagram'),
            ('youtube', 'YouTube'),
            ('x', 'X (Twitter)'),
            ('linkedin', 'LinkedIn'),
            ('wechat', 'WeChat'),
            ('custom', 'Custom'),
        ],
        required=True,
        default='custom',
        tracking=True,
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text()
    icon_url = fields.Char()
    supports_streams = fields.Boolean(default=True)
    supports_posting = fields.Boolean(default=False)
    max_post_length = fields.Integer(default=0, help='0 means no limit.')

    account_ids = fields.One2many('social.hub.account', 'platform_id')
    account_count = fields.Integer(compute='_compute_account_count')

    _platform_code_unique = models.Constraint(
        'UNIQUE(code)',
        'Platform code must be unique.',
    )

    def _compute_account_count(self):
        for platform in self:
            platform.account_count = len(platform.account_ids)
