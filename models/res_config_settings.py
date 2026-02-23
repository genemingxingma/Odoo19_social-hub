from odoo import fields, models


class SocialHubMetaConfig(models.Model):
    _name = 'social.hub.meta.config'
    _description = 'Social Hub Meta Config'

    name = fields.Char(default='Meta API Configuration', required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)

    meta_app_id = fields.Char(string='Meta App ID', required=True)
    meta_app_secret = fields.Char(string='Meta App Secret', required=True)
    meta_graph_version = fields.Char(string='Meta Graph API Version', default='v25.0', required=True)
    meta_scopes = fields.Char(
        string='Meta OAuth Scopes',
        default='pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,business_management',
        required=True,
    )

    _meta_company_unique = models.Constraint(
        'UNIQUE(company_id)',
        'Each company can only have one Meta config record.',
    )
