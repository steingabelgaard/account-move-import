# Copyright 2026 Stein & Gabelgaard ApS
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ZenegyAnalyticMap(models.Model):

    _name = "zenegy.analytic.map"
    _description = "Zenegy Analytic Map"  # TODO

    code = fields.Integer('Department Code', required=True, default=0)
    name = fields.Char('Department Name')
    analytic_account_id = fields.Many2one('account.analytic.account', string='Analytic account')
    analytic_tag_ids = fields.Many2many('account.analytic.tag', string='Analytic tags')
