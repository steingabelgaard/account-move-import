# Copyright 2026 Stein & Gabelgaard ApS
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ZenegyAnalyticMap(models.Model):

    _name = "zenegy.analytic.map"
    _description = "Zenegy Analytic Map"

    def _default_repost_from_account_id(self):
        return self.env['account.account'].search([('code', '=', '74000')], limit=1).id

    code = fields.Integer('Department Code', required=True, default=0)
    name = fields.Char('Department Name')
    analytic_account_id = fields.Many2one('account.analytic.account', string='Analytic account')
    # TODO v19: analytic_tag_ids = fields.Many2many('account.analytic.tag', string='Analytic tags')
    repost_crit_acount_ids = fields.Many2many(
        'account.account', string='Repost sum from accounts',
        default=lambda self: self.env['account.account'].search([('code', 'in', ['73350', '73360', '73365', '73370', '73375', '73380'])])
    )
    repost_from_account_id = fields.Many2one('account.account', string='Repost from account', default=_default_repost_from_account_id)
    repost_to_account_id = fields.Many2one('account.account', string='Repost to account')
    repost_text = fields.Char(
        'Repost text', default='omp - {department}  til viderefakturering {periode}',
        help='Text to use on the repost move lines. Available variables: {department}, {periode}')
