# Copyright 2012-2019 Akretion France (http://www.akretion.com)
# @author Alexis de Lattre <alexis.delattre@akretion.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).


from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_is_zero
from datetime import datetime, date as datelib
import unicodecsv
from tempfile import TemporaryFile
import base64
import zipfile
import logging
import babel

logger = logging.getLogger(__name__)
try:
    import xlrd
except ImportError:
    logger.debug('Cannot import xlrd')


class AccountMoveImport(models.TransientModel):
    _name = "account.move.import"
    _description = "Import account move from CSV file"

    file_to_import = fields.Binary(
        string='File to Import', required=True,
        help="File containing the journal entry(ies) to import.")
    filename = fields.Char()
    file_format = fields.Selection([
        ('genericcsv', 'Generic CSV'),
        ('meilleuregestion', 'MeilleureGestion (Prisme)'),
        ('quadra', 'Quadra (without analytic)'),
        ('extenso', 'In Extenso'),
        ('cielpaye', 'Ciel Paye'),
        ('payfit', 'Payfit'),
        ('fec_txt', 'FEC (text)'),
        ('danloen', 'Danløn'),
        ('c5', 'C5'),
        ('zenegy', 'Zenegy løn'),
        ], string='File Format', required=True, default='danloen',
        help="Select the type of file you are importing.")
    post_move = fields.Boolean(
        string='Post Journal Entry',
        help="If True, the journal entry will be posted after the import.")
    force_journal_id = fields.Many2one(
        'account.journal', string="Force Journal",
        help="Journal in which the journal entry will be created, "
        "even if the file indicate another journal.")
    force_move_ref = fields.Char('Force Reference')
    force_move_line_name = fields.Char('Force Label')
    force_move_date = fields.Date('Force Date')
    file_encoding = fields.Selection([
        ('ascii', 'ASCII'),
        ('latin1', 'ISO 8859-15 (alias Latin1)'),
        ('utf-8', 'UTF-8'),
        ], string='File Encoding', default='utf-8')
    fec_txt_field_separator = fields.Selection([
        ('pipe', '| (pipe)'),
        ('tab', 'Tabulation'),
        ], string='Field Separator', default='pipe')
    # technical fields
    force_move_date_required = fields.Boolean('Force Date Required')
    force_move_line_name_required = fields.Boolean('Force Label Required')
    force_journal_required = fields.Boolean('Force Journal Required')
    account_map_id = fields.Many2one('account.move.import.map', 'Account Mapping')
    col_map_id = fields.Many2one('account.move.import.col.map', 'Column Mapping')
    move_prefix = fields.Char('Label prefix')

    @api.onchange('file_format')
    def file_format_change(self):
        if self.file_format == 'payfit':
            self.force_move_date_required = True
            self.force_move_line_name_required = True
            self.force_journal_required = True
        else:
            self.force_move_date_required = False
            self.force_move_line_name_required = False
            self.force_journal_required = False
        if self.file_format == 'danloen':
            self.force_journal_required = True
            self.force_journal_id = self.env['account.journal'].search([('code', '=', 'MISC')])

    # PIVOT FORMAT
    # [{
    #    'account': {'code': '411000'},
    #    'analytic': {'code': 'ADM'},
    #    'partner': {'ref': '1242'},
    #               # you can use many more keys to match partners
    #    'name': u'label',  # required
    #    'credit': 12.42,
    #    'debit': 0,
    #    'ref': '9804',  # optional
    #    'journal': {'code': 'VT'},
    #    'date': '2017-02-15',  # also accepted in datetime format
    #    'reconcile_ref': 'A1242',  # will be written in import_reconcile
    #                               # and be processed after move line creation
    #    'line': 2,  # Line number for error messages.
    #                # Must be the line number including headers
    # },
    #  2nd line...
    #  3rd line...
    # ]

    def file2pivot(self, fileobj, file_bytes):
        file_format = self.file_format
        if file_format == 'meilleuregestion':
            return self.meilleuregestion2pivot(fileobj)
        elif file_format == 'genericcsv':
            return self.genericcsv2pivot(fileobj)
        elif file_format == 'quadra':
            return self.quadra2pivot(file_bytes)
        elif file_format == 'extenso':
            return self.extenso2pivot(fileobj)
        elif file_format == 'payfit':
            return self.payfit2pivot(file_bytes)
        elif file_format == 'cielpaye':
            return self.cielpaye2pivot(fileobj)
        elif file_format == 'fec_txt':
            return self.fectxt2pivot(fileobj)
        elif file_format == 'danloen':
            return self.danloen2pivot(fileobj)
        elif file_format == 'c5':
            return self.c52pivot(fileobj)
        elif file_format == 'zenegy':
            return self.zenegy2pivot(fileobj)
        else:
            raise UserError(_("You must select a file format."))

    def run_import(self):
        self.ensure_one()
        fileobj = TemporaryFile('wb+')
        file_bytes = base64.b64decode(self.file_to_import)
        logger.info('File size: %s bytes: %s', len(file_bytes), file_bytes)
        fileobj.write(file_bytes)
        fileobj.seek(0)  # We must start reading from the beginning !
        pivot = self.file2pivot(fileobj, file_bytes)
        logger.debug('pivot before update: %s', pivot)
        self.update_pivot(pivot)
        moves = self.create_moves_from_pivot(pivot, post=self.post_move)
        fileobj.seek(0)
        if len(moves) == 1 and zipfile.is_zipfile(fileobj):
            self.archive_zip(moves, fileobj)
        fileobj.close()
        self.reconcile_move_lines(moves)
        action = {
            'name': _('Imported Journal Entries'),
            'res_model': 'account.move',
            'type': 'ir.actions.act_window',
            'nodestroy': False,
            'target': 'current',
            }

        if len(moves) == 1:
            action.update({
                'view_mode': 'form,list',
                'res_id': moves[0].id,
                })
        else:
            action.update({
                'view_mode': 'list,form',
                'domain': [('id', 'in', moves.ids)],
                })
        return action

    def archive_zip(self, moves, fileobj):
        with zipfile.ZipFile(fileobj) as myzip:
            for filename in myzip.namelist():
                with myzip.open(filename) as myfile:
                    att = self.env["ir.attachment"].create(
                        {
                            "res_model": "account.move",
                            "res_id": moves.id,
                            "name": filename,
                            "datas": base64.b64encode(myfile.read()),
                        }
                    )
                    if filename.endswith('bogforing_total.pdf'):
                        att.register_as_main_attachment(force=False)

    def update_pivot(self, pivot):
        force_move_date = self.force_move_date
        force_move_ref = self.force_move_ref
        force_move_line_name = self.force_move_line_name
        force_journal_code = self.force_journal_id and self.force_journal_id.code or False
        for l in pivot:
            if force_move_date:
                l['date'] = force_move_date
            if force_move_line_name:
                l['name'] = force_move_line_name
            if force_move_ref:
                l['ref'] = force_move_ref
            if force_journal:
                l['journal'] = {'recordset': force_journal}
            if isinstance(l.get('date'), datetime):
                l['date'] = fields.Date.to_string(l['date'])
            if not l['credit']:
                l['credit'] = 0.0
            if not l['debit']:
                l['debit'] = 0.0

    def extenso2pivot(self, fileobj):
        fieldnames = [
            'journal', 'date', False, 'account', False, False, False, False,
            'debit', 'credit']
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter='\t',
            quoting=False,
            encoding='utf-8')
        res = []
        i = 0
        for l in reader:
            i += 1
            l['credit'] = l['credit'] or '0'
            l['debit'] = l['debit'] or '0'
            vals = {
                'journal': {'code': l['journal']},
                'account': {'code': l['account']},
                'credit': float(l['credit'].replace(',', '.')),
                'debit': float(l['debit'].replace(',', '.')),
                'date': datetime.strptime(l['date'], '%d%m%Y'),
                'line': i,
            }
            res.append(vals)
        return res

    def cielpaye2pivot(self, fileobj):
        fieldnames = [
            False, 'journal', 'date', 'account', False, 'amount', 'sign',
            False, 'name', False]
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter='\t',
            quoting=unicodecsv.QUOTE_MINIMAL,
            encoding='utf-8')
        res = []
        i = 0
        for l in reader:
            i += 1
            # skip non-move lines
            if l.get('date') and l.get('name') and l.get('amount'):
                amount = float(l['amount'].replace(',', '.'))
                vals = {
                    'journal': {'code': l['journal']},
                    'account': {'code': l['account']},
                    'credit': l['sign'] == 'C' and amount or 0,
                    'debit': l['sign'] == 'D' and amount or 0,
                    'date': datetime.strptime(l['date'], '%d/%m/%Y'),
                    'name': l['name'],
                    'line': i,
                }
                res.append(vals)
        return res

    def fectxt2pivot(self, fileobj):
        fieldnames = [
            'journal', False, False, 'date', 'account', 'name',
            False, False,  # CompAuxNum|CompAuxLib
            'ref', False, 'name', 'debit', 'credit',
            'reconcile_ref', False, False, False, False]
        if self.fec_txt_field_separator == 'pipe':
            delimiter = '|'
        elif self.fec_txt_field_separator == 'tab':
            delimiter = '\t'
        else:
            raise UserError(_('You must select a field separator.'))
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter=delimiter,
            encoding=self.file_encoding)
        res = []
        i = 0
        for l in reader:
            i += 1
            # Skip header line
            if i == 1:
                continue
            l['credit'] = l['credit'] or '0'
            l['debit'] = l['debit'] or '0'
            vals = {
                'journal': {'code': l['journal']},
                'account': {'code': l['account']},
                #    'partner': {'ref': '1242'},
                'credit': float(l['credit'].replace(',', '.')),
                'debit': float(l['debit'].replace(',', '.')),
                'date': datetime.strptime(l['date'], '%Y%m%d'),
                'name': l['name'],
                'reconcile_ref': l['reconcile_ref'],
                'line': i,
            }
            res.append(vals)
        return res

    def genericcsv2pivot(self, fileobj):
        # Prisme
        fieldnames = [
            'date', 'journal', 'account', 'partner',
            'analytic', 'name', 'debit', 'credit',
            ]
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter=',',
            quotechar='"',
            quoting=unicodecsv.QUOTE_MINIMAL,
            encoding='utf-8')
        res = []
        i = 0
        for l in reader:
            i += 1
            vals = {
                'journal': {'code': l['journal']},
                'account': {'code': l['account']},
                'credit': float(l['credit'] or 0),
                'debit': float(l['debit'] or 0),
                'date': datetime.strptime(l['date'], '%d/%m/%Y'),
                'name': l['name'],
                'line': i,
                }
            if l['analytic']:
                vals['analytic'] = {'code': l['analytic']}
            if l['partner']:
                vals['partner'] = {'ref': l['partner']}
            res.append(vals)
        return res

    def danloen2pivot(self, fileobj):
        res = []
        if zipfile.is_zipfile(fileobj):
            with zipfile.ZipFile(fileobj) as myzip:
                for filename in myzip.namelist():
                    logger.info('FILE: %s', filename)
                    if filename.endswith('_danlonfinans.txt'):
                        res = self._danloen2pivot(myzip.open(filename))
        else:
            logger.info("Processing file as a simple text file: %s", fileobj)
            fileobj.seek(0)
            res = self._danloen2pivot(fileobj)
        return res

    def _danloen2pivot(self, fileobj):
        fieldnames = [
            False, 'date', False, 'account', False, 'amount', 'name', 'period']
        #wrapper = io.TextIOWrapper(fileobj, encoding='utf-8')
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter=';',
            quoting=unicodecsv.QUOTE_MINIMAL,
            encoding='utf-8'
        )
        res = []
        i = 0
        # logger.info('Processing Reader: %s', reader)
        for l in reader:
            # logger.info('LINE: %s', l)
            if len(l['account']) > 2:
                i += 1
                amount = float(l['amount'].replace('.', '').replace(',', '.'))
                if amount > 0:
                    debit = amount
                    credit = 0
                else:
                    debit = 0
                    credit = - amount
                vals = {
                    'account': {'code': l['account']},
                    'name': l['name'] + ' - ' + l['period'],
                    'credit': credit,
                    'debit': debit,
                    'date': datetime.strptime(l['date'], '%Y-%m-%d'),
                    'line': i,
                    'ref': 'Løn ' + l['period']
                }
                res.append(vals)
        return res

    def zenegy2pivot(self, fileobj):
        # fieldnames = [
        #    'number', 'date', False, 'account', False, 'amount', 'name', 'period']
        aa = self.env['account.analytic.account']
        line1 = fileobj.readline().decode('iso-8859-1')
        logger.info('LOEN: %s', line1)
        if line1.startswith('Lønkørsels ID;CVR nummer;Periode fra;Periode til;Dispositionsdato;Afdelingsnavn;Konto;Tekst;Debet;Kredit'):
            fileobj.seek(0)
        elif line1.startswith('Lønkørsels ID;Periode fra;Periode til;Dispositionsdato;Afdelingsnavn;Konto;Tekst;Debet;Kredit'):
            fileobj.seek(0)
        elif line1.startswith('Lønkørsels-ID;CVR nummer;Periode fra;Periode til;Dispositionsdato;Afdelingsnavn;Konto;Tekst;Debet;Kredit'):
            fileobj.seek(0)
        elif line1.startswith('Lønkørsels-ID;Periode fra;Periode til;Dispositionsdato;Afdelingsnavn;Konto;Tekst;Debet;Kredit'):
            fileobj.seek(0)
        elif not line1.startswith('sep=;'):
            raise UserError(_("This is not a Zenegy Payroll file."))
        reader = unicodecsv.DictReader(
            fileobj,
            delimiter=';',
            encoding='iso-8859-1')
        res = []
        i = 0
        for l in reader:
            i += 1
            if 'Lønkørsels ID' in l:
                loen_id_key = 'Lønkørsels ID'
            else:
                loen_id_key = 'Lønkørsels-ID'
            if l[loen_id_key].isdigit():
                debit = 0
                credit = 0
                credit2 = 0
                if l['Debet']:
                    debit = float(l['Debet'].replace('.', '').replace(',', '.'))
                if l['Kredit']:
                    credit = float(l['Kredit'].replace('.', '').replace(',', '.'))
                if credit and debit:
                    credit2 = credit
                    credit = 0
                vals = {
                    'account': {'code': l['Konto']},
                    'name': l['Tekst'],
                    'credit': credit,
                    'debit': debit,
                    'date': datetime.strptime(l['Dispositionsdato'], '%d-%m-%Y'),
                    'line': i,
                    'ref': 'Løn #%s: %s %s - %s' % (l[loen_id_key], l['Afdelingsnavn'], l['Periode fra'], l['Periode til'])
                }
                if l['Afdelingsnr.'] and l['Afdelingsnr.'].isdigit() and l['Afdelingsnavn']:
                    zenegy_map = self.env['zenegy.analytic.map'].search([('code', '=', int(l['Afdelingsnr.']))], limit=1)
                    if zenegy_map:
                        vals['zenegy_analytic_map'] = zenegy_map
                        vals['analytic_account_id'] = zenegy_map.analytic_account_id.id
                        if zenegy_map.analytic_tag_ids:
                            vals['analytic_tag_ids'] = [(6, 0, zenegy_map.analytic_tag_ids.ids)]
                    else:
                        analytic = aa.search([('name', '=', l['Afdelingsnavn'])])
                        if analytic:
                            vals['analytic_account_id'] = analytic.id
                        self.env['zenegy.analytic.map'].create({
                            'code': int(l['Afdelingsnr.']),
                            'name': l['Afdelingsnavn'],
                            'analytic_account_id': analytic.id if analytic else False,
                        })
                logger.info('VALS: %s', vals)
                res.append(vals)
                if credit2:
                    vals2 = vals.copy()
                    vals2['debit'] = 0
                    vals2['credit'] = credit2
                    res.append(vals2)
        return res

    def meilleuregestion2pivot(self, fileobj):
        fieldnames = [
            'trasha', 'trashb', 'journal', 'trashd', 'trashe',
            'trashf', 'trashg', 'date', 'trashi', 'trashj', 'trashk',
            'trashl', 'trashm', 'trashn', 'account', 'trashp',
            'trashq', 'amount', 'trashs', 'sign', 'trashu',
            'trashv', 'name',
            'trashx', 'trashy', 'trashz', 'trashaa', 'trashab',
            'trashac', 'trashad', 'trashae', 'analytic']
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter=';',
            quoting=False,
            encoding='latin1')
        res = []
        i = 0
        for l in reader:
            i += 1
            if i == 1:
                continue
            amount = float(l['amount'].replace(',', '.'))
            credit = l['sign'] == 'C' and amount or False
            debit = l['sign'] == 'D' and amount or False
            ana = l.get('analytic') and {'code': l.get('analytic')} or False
            vals = {
                'journal': {'code': l['journal']},
                'account': {'code': l['account']},
                'analytic': ana,
                'credit': credit,
                'debit': debit,
                'date': datetime.strptime(l['date'], '%y%m%d'),
                'name': l['name'],
                'line': i,
            }
            res.append(vals)
        return res

    def quadra2pivot(self, file_bytes):
        i = 0
        res = []
        file_str = file_bytes.decode(self.file_encoding)
        for l in file_str.split('\n'):
            i += 1
            if len(l) < 54:
                continue
            if l[0] == 'M' and l[41] in ('C', 'D'):
                amount_cents = int(l[42:55])
                amount = amount_cents / 100.0
                vals = {
                    'journal': {'code': l[9:11]},
                    'account': {'code': l[1:9]},
                    'credit': l[41] == 'C' and amount or False,
                    'debit': l[41] == 'D' and amount or False,
                    'date': datetime.strptime(l[14:20], '%d%m%y'),
                    'name': l[21:41],
                    'line': i,
                }
                res.append(vals)
        return res

    def payfit2pivot(self, file_bytes):
        wb = xlrd.open_workbook(file_contents=file_bytes)
        sh1 = wb.sheet_by_index(1)
        i = 0
        res = []
        name = 'Paye'
        for rownum in range(sh1.nrows):
            row = sh1.row_values(rownum)
            i += 1
            if i == 1:
                continue
            if not row[0]:
                continue
            account = str(row[0])
            if '.' in account:
                account = account.split('.')[0]
            if not account[0].isdigit():
                continue
            analytic = str(row[3])
            vals = {
                'account': {'code': account},
                'name': name,
                'debit': float(row[5] or 0.0),
                'credit': float(row[6] or 0.0),
                'line': i,
            }
            if analytic:
                vals['analytic'] = {'code': analytic}
            res.append(vals)
        return res

    def c52pivot(self, fileobj):

        def get_col(col):
            return ord(col[0]) - 65

        def take_voucher(elem):
            return elem[get_col(self.col_map_id.voucher_fld)]

        org_code = self.env.user.company_id.partner_id.organization_id.organization_code
        reader = unicodecsv.reader(
            fileobj,
            delimiter=self.col_map_id.delimiter,
            quoting=unicodecsv.QUOTE_MINIMAL,
            encoding=self.col_map_id.encoding)
        res = []
        i = 0
        lines = []
        for l in reader:
            i += 1
            if i > self.col_map_id.skip_lines:
                lines.append(l)
        lines.sort(key=take_voucher)
        for l in lines:

            text = l[get_col(self.col_map_id.text_fld)]
            if (not(text and text.strip())):
                continue
            if self.col_map_id.amount_fld:
                amount_txt = l[get_col(self.col_map_id.amount_fld)]
                logger.info('AMOUNT_TXT: [%s]', amount_txt)
                if (not(amount_txt and amount_txt.strip())):
                    continue
                amount = float(amount_txt.replace('.', '').replace(',', '.'))
                if amount > 0:
                    debit = amount
                    credit = 0
                else:
                    debit = 0
                    credit = - amount
            else:
                debit = float(l[get_col(self.col_map_id.debit_fld)].replace('.', '').replace(',', '.'))
                credit = float(l[get_col(self.col_map_id.credit_fld)].replace('.', '').replace(',', '.'))

            # Partner search
            partner = False
            number = [int(s) for s in l[get_col(self.col_map_id.text_fld)].split() if s.isdigit()]
            if number:
                member_number = '%s%s' % (org_code, number[0])
                partner = self.env['res.partner'].with_context(active_test=False).search([('member_number', '=', member_number)])

            vals = {
                'account': {'code': l[get_col(self.col_map_id.account_fld)]},
                'name': l[get_col(self.col_map_id.text_fld)],
                'credit': credit,
                'debit': debit,
                'date': datetime.strptime(l[get_col(self.col_map_id.date_fld)].replace('PR', '01'), self.col_map_id.date_format),
                'line': i,
                'ref': l[get_col(self.col_map_id.voucher_fld)]
            }
            if partner:
                vals['partner_id'] = partner.commercial_partner_id.id
            res.append(vals)
        return res

    def _prepare_partner_speeddict(self, company_id):
        speeddict = {}
        partner_sr = self.env['res.partner'].with_context(active_test=False).search_read(
            [
                '|',
                ('company_id', '=', company_id),
                ('company_id', '=', False),
                ('ref', '!=', False),
                ('parent_id', '=', False),
            ],
            ['ref'])
        for l in partner_sr:
            speeddict[l['ref'].upper()] = l['id']
        return speeddict

    def _prepare_speeddict(self, company_id):
        speeddict = {
            "partner": self._prepare_partner_speeddict(company_id),
            "journal": {},
            "account": {},
            "analytic": {},
            }
        acc_sr = self.env['account.account'].with_company(company_id).search_read([
            ('company_ids', 'in', company_id),
            ('deprecated', '=', False)], ['code'])
        for l in acc_sr:
            speeddict['account'][l['code'].upper()] = l['id']
        aacc_sr = self.env['account.analytic.account'].search_read(
            [('company_id', 'in', (company_id, False)), ('code', '!=', False)],
            ['code'])
        for l in aacc_sr:
            speeddict['analytic'][l['code'].upper()] = l['id']
        journal_sr = self.env['account.journal'].search_read([
            ('company_id', '=', company_id)], ['code'])
        for l in journal_sr:
            speeddict['journal'][l['code'].upper()] = l['id']
        return speeddict

    def create_moves_from_pivot(self, pivot, post=False):
        logger.info('Final pivot: %s', pivot)
        amo = self.env['account.move']
        speeddict = self._prepare_speeddict(self.env.company.id)
        key2label = {
            'journal': _('journal codes'),
            'account': _('account codes'),
            'partner': _('partner reference'),
            'analytic': _('analytic codes'),
            }
        errors = {'other': []}
        for key in key2label.keys():
            errors[key] = {}
        if self.account_map_id:
            acc_speed_dict = self.account_map_id._prepare_account_speed_dict()
        else:
            acc_speed_dict = speeddict['account']
        # MATCH what needs to be matched... + CHECKS
        for l in pivot:
            assert l.get('line') and isinstance(l.get('line'), int),\
                'missing line number'
            if l['account']['code'] in acc_speed_dict:
                l['account_id'] = acc_speed_dict[l['account']['code']]
            if not l.get('account_id'):
                # Match when import = 61100000 and Odoo has 611000
                acc_code_tmp = l['account']
                while acc_code_tmp and acc_code_tmp[-1] == '0':
                    acc_code_tmp = acc_code_tmp[:-1]
                    if acc_code_tmp and acc_code_tmp in acc_speed_dict:
                        l['account_id'] = acc_speed_dict[acc_code_tmp]
                        break
            if not l.get('account_id'):
                # Match when import = 611000 and Odoo has 611000XX
                for code, account_id in acc_speed_dict.items():
                    if code.startswith(l['account']):
                        logger.warning(
                            "Approximate match: import account %s has been matched "
                            "with Odoo account %s" % (l['account'], code))
                        l['account_id'] = account_id
                        break
            if not l.get('account_id'):
                errors['account'].setdefault(l['account'], []).append(l['line'])
            if l.get('partner'):
                if l['partner'] in speeddict['partner']:
                    l['partner_id'] = speeddict['partner'][l['partner']]
                else:
                    errors['partner'].setdefault(l['partner'], []).append(l['line'])
            if l.get('partner'):
                if l['partner'] in speeddict['partner']:
                    l['partner_id'] = speeddict['partner'][l['partner']]
                else:
                    errors['partner'].setdefault(l['partner'], []).append(l['line'])
            if l.get('analytic'):
                l['analytic_distribution'] = {}
                for ana_entry in l['analytic'].split('|'):
                    ana_entry = ana_entry.strip()
                    if ana_entry:
                        ana_entry_split = ana_entry.split(':')
                        if len(ana_entry_split) == 1:
                            ana_account_code = ana_entry_split[0].strip()
                            ana_pct = 100
                        elif len(ana_entry_split) > 1:
                            ana_account_code = ':'.join(ana_entry_split[:-1]).strip()
                            ana_pct_str = ana_entry_split[-1]
                            ana_pct_str_ready = ana_pct_str.replace(',', '.')
                            try:
                                ana_pct = float(ana_pct_str_ready)
                            except Exception:
                                errors['other'].append("Line %d: wrong analytic percentage: '%s' is not a number." % (l['line'], ana_pct_str))
                                ana_pct = 1
                            if ana_pct < 0 or ana_pct > 100:
                                errors['other'].append("Line %d: wrong analytic percentage: '%s' is not between 0 and 100." % (l['line'], ana_pct_str))
                        if ana_account_code in speeddict['analytic']:
                            l['analytic_distribution'][speeddict['analytic'][ana_account_code]] = ana_pct
                        else:
                            errors['analytic'].setdefault(ana_account_code, []).append(l['line'])

            if l['journal'] in speeddict['journal']:
                l['journal_id'] = speeddict['journal'][l['journal']]
            else:
                errors['journal'].setdefault(l['journal'], []).append(l['line'])
            if not l.get('name'):
                errors['other'].append(_(
                    'Line %d: missing label.') % l['line'])
            if not l.get('date'):
                errors['other'].append(_(
                    'Line %d: missing date.') % l['line'])
            else:
                if not isinstance(l.get('date'), datelib):
                    try:
                        l['date'] = datetime.strptime(l['date'], '%Y-%m-%d')
                    except Exception:
                        errors['other'].append(_(
                            'Line %d: bad date format %s') % (l['line'], l['date']))
            if not isinstance(l.get('credit'), (float, int)):
                errors['other'].append(_(
                    'Line %d: bad value for credit (%s).')
                    % (l['line'], l['credit']))
            if not isinstance(l.get('debit'), (float, int)):
                errors['other'].append(_(
                    'Line %d: bad value for debit (%s).')
                    % (l['line'], l['debit']))
            # test that they don't have both a value

        # LIST OF ERRORS
        msg = ''
        for key, label in key2label.items():
            if errors[key]:
                errors_key_sorted = sorted(errors[key].items(), key=lambda x: x[0])
                msg += _("List of %s that don't exist in Odoo:\n%s\n\n") % (
                    label,
                    '\n'.join([
                        '- %s : line(s) %s' % (code, ', '.join([str(i) for i in lines]))
                        for (code, lines) in errors_key_sorted]))
        if errors['other']:
            msg += _('List of misc errors:\n%s') % (
                '\n'.join(['- %s' % e for e in errors['other']]))
        if msg:
            raise UserError(msg)

        # EXTRACT MOVES
        moves = []
        cur_journal_id = False
        cur_ref = False
        cur_date = False
        cur_balance = 0.0
        prec = self.env.user.company_id.currency_id.rounding
        cur_move = {}
        cur_zenegy_analytic_map = False
        for l in pivot:
            ref = l.get('ref', False)
            if (
                    cur_ref == ref and
                    cur_journal_id == l['journal_id'] and
                    cur_date == l['date'] and
                    ref
            ):
                # not float_is_zero(cur_balance, precision_rounding=prec)):
                # append to current move
                cur_move['line_ids'].append((0, 0, self._prepare_move_line(l)))
            else:
                # new move
                if moves and not float_is_zero(
                        cur_balance, precision_rounding=prec):
                    raise UserError(_(
                        "The journal entry that ends on line %d (voucher %s)is not "
                        "balanced (balance is %s).")
                        % (l['line'] - 1, l['ref'], cur_balance))
                if cur_move:
                    if not len(cur_move['line_ids']) > 1:
                        raise UserError(_('move should have more than 1 line (%s) %d') % (cur_ref, len(cur_move['line_ids'])))
                    if cur_zenegy_analytic_map and cur_zenegy_analytic_map.repost_crit_acount_ids and cur_zenegy_analytic_map.repost_to_account_id and cur_zenegy_analytic_map.repost_from_account_id:
                        repost_debit = 0
                        repost_credit = 0
                        for line in cur_move['line_ids']:
                            if line[2]['account_id'] in cur_zenegy_analytic_map.repost_crit_acount_ids.ids:
                                repost_debit += line[2]['debit']
                                repost_credit += line[2]['credit']
                        if repost_debit or repost_credit:
                            repost_vals = [
                                {
                                    'account_id': cur_zenegy_analytic_map.repost_to_account_id.id,
                                    'debit': repost_debit,
                                    'credit': repost_credit,
                                    'name': cur_zenegy_analytic_map.repost_text.format(
                                        department=cur_zenegy_analytic_map.name,
                                        periode=babel.dates.format_date(cur_date, format='MMMM yyyy', locale=self.env.user.lang),
                                    ),
                                    'date': cur_date,
                                    'analytic_tag_ids': [(6, 0, cur_zenegy_analytic_map.analytic_tag_ids.ids)] if cur_zenegy_analytic_map.analytic_tag_ids else False,
                                },
                                {
                                    'account_id': cur_zenegy_analytic_map.repost_from_account_id.id,
                                    'debit': repost_credit,
                                    'credit': repost_debit,
                                    'name': cur_zenegy_analytic_map.repost_text.format(
                                        department=cur_zenegy_analytic_map.name,
                                        periode=babel.dates.format_date(cur_date, format='MMMM yyyy', locale=self.env.user.lang),
                                    ),
                                    'date': cur_date,
                                    'analytic_tag_ids': [(6, 0, cur_zenegy_analytic_map.analytic_tag_ids.ids)] if cur_zenegy_analytic_map.analytic_tag_ids else False,
                                }
                            ]
                            cur_move['line_ids'].append((0, 0, repost_vals[0]))
                            cur_move['line_ids'].append((0, 0, repost_vals[1]))
                    moves.append(cur_move)
                cur_move = self._prepare_move(l)
                cur_move['line_ids'] = [(0, 0, self._prepare_move_line(l))]
                cur_date = l['date']
                logger.info('REF: %s, JOURNAL: %s, DATE: %s - %s', ref, l['journal_id'], cur_date, type(cur_date))
                cur_ref = ref
                cur_zenegy_analytic_map = l.get('zenegy_analytic_map', False)
                cur_journal_id = l['journal_id']
            cur_balance += l['credit'] - l['debit']
        if cur_move:
            moves.append(cur_move)
        if not float_is_zero(cur_balance, precision_rounding=prec):
            raise UserError(_(
                "The journal entry that ends on the last line is not "
                "balanced (balance is %s).") % cur_balance)
        rmoves = self.env['account.move']
        for move in moves:
            logger.info('Creating move: %s', move['ref'])
            for line in move['line_ids']:
                logger.info('    with line: %s (%s, %s)', line[2]['name'], line[2]['debit'], line[2]['credit'])
            rmoves += amo.create(move)
        logger.info(
            'Account moves IDs %s created via file import' % rmoves.ids)
        if post:
            rmoves.post()
        return rmoves

    def _prepare_move(self, pivot_line):
        vals = {
            'journal_id': pivot_line['journal_id'],
            'ref': pivot_line.get('ref'),
            'date': pivot_line['date'],
            }
        if self.move_prefix:
            vals['name'] = "%s-%s" % (self.move_prefix, pivot_line.get('ref'))
        return vals

    def _prepare_move_line(self, pivot_line):
        vals = {
            'credit': pivot_line['credit'],
            'debit': pivot_line['debit'],
            'name': pivot_line['name'],
            'partner_id': pivot_line.get('partner_id'),
            'account_id': pivot_line['account_id'],
            'analytic_distribution': pivot_line.get('analytic_distribution'),
            'import_reconcile': pivot_line.get('reconcile_ref'),
            }
        if pivot_line.get('analytic_tag_ids'):
            vals['analytic_tag_ids'] = pivot_line.get('analytic_tag_ids')
        return vals

    def reconcile_move_lines(self, moves):
        prec = self.env.user.company_id.currency_id.rounding
        logger.info('Start to reconcile imported moves')
        lines = self.env['account.move.line'].search([
            ('move_id', 'in', moves.ids),
            ('import_reconcile', '!=', False),
            ])
        torec = {}  # key = reconcile mark, value = movelines_recordset
        for line in lines:
            if line.import_reconcile in torec:
                torec[line.import_reconcile] += line
            else:
                torec[line.import_reconcile] = line
        for rec_ref, lines_to_rec in torec.items():
            if len(lines_to_rec) < 2:
                logger.warning(
                    "Skip reconcile of ref '%s' because "
                    "this ref is only on 1 move line", rec_ref)
                continue
            total = 0.0
            accounts = {}
            partners = {}
            for line in lines_to_rec:
                total += line.credit
                total -= line.debit
                accounts[line.account_id] = True
                partners[line.partner_id.id or False] = True
            if not float_is_zero(total, precision_digits=prec):
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref are not balanced (%s)", rec_ref, total)
                continue
            if len(accounts) > 1:
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref have different accounts (%s)",
                    rec_ref, ', '.join([acc.code for acc in accounts.keys()]))
                continue
            if not list(accounts)[0].reconcile:
                logger.warning(
                    "Skip reconcile of ref '%s' because the account '%s' "
                    "is not configured with 'Allow Reconciliation'",
                    rec_ref, list(accounts)[0].display_name)
                continue
            if len(partners) > 1:
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref have different partners (IDs %s)",
                    rec_ref, ', '.join(partners.keys()))
                continue
            lines_to_rec.reconcile()
        logger.info('Reconcile imported moves finished')
