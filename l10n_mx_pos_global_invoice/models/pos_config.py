from odoo import fields, models


class PosConfig(models.Model):
    _inherit = 'pos.config'

    global_customer_id = fields.Many2one(
        comodel_name="res.partner",
        string="Global customer",
        required=False,
    )
    create_global_invoice = fields.Boolean(
        string="Create global invoice",
    )
    global_invoice_method = fields.Selection(
        string="Method",
        selection=[
            ('manual', 'Manual'),
            ('automatic', 'Automatic'),
        ],
    )
    global_journal_id = fields.Many2one(
        'account.journal', string='Diario',)
