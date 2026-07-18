from odoo import api, fields, models

from .secret_cipher import decrypt_secret, encrypt_secret, migrate_legacy_secrets


class SocialHubMetaConfig(models.Model):
    _name = 'social.hub.meta.config'
    _description = 'Social Hub Meta Config'
    _check_company_auto = True

    name = fields.Char(default='Meta API Configuration', required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    meta_app_id = fields.Char(string='Meta App ID', required=True)
    meta_app_secret_encrypted = fields.Text(
        string='Encrypted Meta App Secret',
        copy=False,
        groups='social_hub.group_social_hub_manager',
    )
    meta_app_secret = fields.Char(
        string='Meta App Secret',
        compute='_compute_meta_app_secret',
        inverse='_inverse_meta_app_secret',
        groups='social_hub.group_social_hub_manager',
    )
    meta_graph_version = fields.Char(string='Meta Graph API Version', default='v25.0', required=True)
    meta_scopes = fields.Char(
        string='Meta OAuth Scopes',
        default='pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish',
        required=True,
    )

    _meta_company_unique = models.Constraint(
        'UNIQUE(company_id)',
        'Each company can only have one Meta config record.',
    )

    def init(self):
        migrate_legacy_secrets(
            self.env,
            self._table,
            {'meta_app_secret': 'meta_app_secret_encrypted'},
        )

    @api.model_create_multi
    def create(self, vals_list):
        prepared_values = []
        for values in vals_list:
            values = dict(values)
            if 'meta_app_secret' in values:
                values['meta_app_secret_encrypted'] = encrypt_secret(values.pop('meta_app_secret'))
            prepared_values.append(values)
        return super().create(prepared_values)

    def write(self, values):
        values = dict(values)
        if 'meta_app_secret' in values:
            values['meta_app_secret_encrypted'] = encrypt_secret(values.pop('meta_app_secret'))
        return super().write(values)

    def _compute_meta_app_secret(self):
        for config in self:
            config.meta_app_secret = decrypt_secret(config.meta_app_secret_encrypted)

    def _inverse_meta_app_secret(self):
        for config in self:
            config.meta_app_secret_encrypted = encrypt_secret(config.meta_app_secret)
