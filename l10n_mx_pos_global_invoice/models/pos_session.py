from odoo import models, fields, _
from collections import defaultdict
from odoo.exceptions import UserError, AccessError
from odoo.tools import float_is_zero, float_compare

class PosSession(models.Model):
    _inherit = 'pos.session'

    global_invoice_id = fields.Many2one(
        comodel_name="account.move",
        string="Global invoice",
        required=False,
    )
    has_global_invoice = fields.Boolean(
        string="Global invoiced",
    )

    def _create_account_move(self):
        """ Create global invoice, invoice line and payments records for this session.
        Side-effects include:
            - setting self.move_id to the created account.move record
            - creating and validating account.bank.statement for cash payments
            - reconciling cash receivable lines, invoice receivable lines and stock
            output lines
        """
        if self.config_id.create_global_invoice and \
                self.config_id.global_invoice_method == 'automatic':
            journal = self.config_id.journal_id
            # Passing default_journal_id for the calculation of default currency of
            # account move
            # See _get_default_currency in the account/account_move.py.
            if not self.config_id.global_customer_id:
                raise UserError(_('Global customer not detected, '
                                  'please configure global customer on pos config'))
            global_customer = self.config_id.global_customer_id
            account_move = self.env['account.move'].with_context(
                default_journal_id=journal.id).create({
                    'journal_id': journal.id,
                    'date': fields.Date.context_today(self),
                    'ref': self.name,
                })
            global_invoice = self.env['account.move'].with_context(
                default_journal_id=journal.id).create({
                    'journal_id': journal.id,
                    'date': fields.Date.context_today(self),
                    'ref': self.name,
                    'partner_id': self.config_id.global_customer_id.id,
                    'move_type': 'out_invoice',
                })
            self.write({
                'move_id': account_move.id,
                'global_invoice_id': global_invoice.id,
            })
            data = {}
            data = pos_session._accumulate_amounts_global_invoice(data)
            data = pos_session._create_non_reconciliable_move_lines(data)
            data = pos_session._create_bank_payment_moves(data)
            data = pos_session._create_cash_statement_lines_and_cash_move_lines(data)
            data = pos_session._create_invoice_receivable_lines(data)
            data = pos_session._create_stock_output_lines(data)
            balancing_account= False
            amount_to_balance= 0
            data = pos_session._create_balancing_line(data,balancing_account,amount_to_balance)
            line_receivable = self.env['account.move.line'].search([
                ('account_id', '=', global_customer.property_account_receivable_id.id),
                ('move_id', '=', account_move.id),
                ('name', 'like', _('Sales:'))
            ])  
            if account_move.line_ids:
                print("account_move 405",account_move.name, account_move.id, account_move.line_ids)
                lines = pos_session._line_correction_amounts_global_invoice(account_move.line_ids)
                self.env['account.move.line'].with_context(check_move_validity=False).create(lines)
                self.env['account.move.line'].search([('move_id', '=', account_move.id), ('check_global_invoice', '=', False)]).unlink()
                account_move._post()
            data = pos_session._reconcile_account_move_lines(data)
            print("total_paid_orders",data.get('total_paid_orders'))
            print("data.get('invoice_lines')",data.get('invoice_lines'))
            global_invoice.write({
                'invoice_line_ids': [(0, None, invoice_line) for invoice_line in
                                     data.get('invoice_lines')],
            }) 
            if global_invoice.line_ids:
                global_invoice._post()   
                if global_invoice.line_ids and global_invoice.state == 'posted':
                    move_lines = self.env['account.move.line'].search([
                    ('move_id', 'in', (global_invoice.id, account_move.id)),
                    ('account_id', '=', pos_session.config_id.global_customer_id.property_account_receivable_id.id),
                    ('name', '!=', 'From invoiced orders')
                    ]).reconcile()
                    # print("move_lines",move_lines)
                    # for line in move_lines:
                    #     print("line",line)
                    #     line.reconcile()
                 
            self.write({'has_global_invoice': True})
            
                    
        else:
            super(PosSession, self)._create_account_move()

    def _accumulate_amounts_global_invoice(self, data):
        # Accumulate the amounts for each accounting lines group
        # Each dict maps `key` -> `amounts`, where `key` is the group key.
        # E.g. `combine_receivables_bank` is derived from pos.payment records
        # in the self.order_ids with group key of the `payment_method_id`
        # field of the pos.payment record.
        amounts = lambda: {'amount': 0.0, 'amount_converted': 0.0}
        tax_amounts = lambda: {'amount': 0.0, 'amount_converted': 0.0, 'base_amount': 0.0, 'base_amount_converted': 0.0}
        split_receivables_bank = defaultdict(amounts)
        split_receivables_cash = defaultdict(amounts)
        split_receivables_pay_later = defaultdict(amounts)
        combine_receivables_bank = defaultdict(amounts)
        combine_receivables_cash = defaultdict(amounts)
        combine_receivables_pay_later = defaultdict(amounts)
        combine_invoice_receivables = defaultdict(amounts)
        split_invoice_receivables = defaultdict(amounts)
        sales = defaultdict(amounts)
        taxes = defaultdict(tax_amounts)
        stock_expense = defaultdict(amounts)
        stock_return = defaultdict(amounts)
        stock_output = defaultdict(amounts)
        rounding_difference = {'amount': 0.0, 'amount_converted': 0.0}
        # Track the receivable lines of the order's invoice payment moves for reconciliation
        # These receivable lines are reconciled to the corresponding invoice receivable lines
        # of this session's move_id.
        combine_inv_payment_receivable_lines = defaultdict(lambda: self.env['account.move.line'])
        split_inv_payment_receivable_lines = defaultdict(lambda: self.env['account.move.line'])
        rounded_globally = self.company_id.tax_calculation_rounding_method == 'round_globally'
        pos_receivable_account = self.company_id.account_default_pos_receivable_account_id
        currency_rounding = self.currency_id.rounding
        invoice_lines = []
        total_paid_orders = 0
        for order in self.order_ids:
            order_is_invoiced = order.is_invoiced
            for payment in order.payment_ids:
                amount = payment.amount
                if float_is_zero(amount, precision_rounding=currency_rounding):
                    continue
                date = payment.payment_date
                payment_method = payment.payment_method_id
                is_split_payment = payment.payment_method_id.split_transactions
                payment_type = payment_method.type

                # If not pay_later, we create the receivable vals for both invoiced and uninvoiced orders.
                #   Separate the split and aggregated payments.
                # Moreover, if the order is invoiced, we create the pos receivable vals that will balance the
                # pos receivable lines from the invoice payments.
                if payment_type != 'pay_later':
                    if is_split_payment and payment_type == 'cash':
                        split_receivables_cash[payment] = self._update_amounts(split_receivables_cash[payment], {'amount': amount}, date)
                    elif not is_split_payment and payment_type == 'cash':
                        combine_receivables_cash[payment_method] = self._update_amounts(combine_receivables_cash[payment_method], {'amount': amount}, date)
                    elif is_split_payment and payment_type == 'bank':
                        split_receivables_bank[payment] = self._update_amounts(split_receivables_bank[payment], {'amount': amount}, date)
                    elif not is_split_payment and payment_type == 'bank':
                        combine_receivables_bank[payment_method] = self._update_amounts(combine_receivables_bank[payment_method], {'amount': amount}, date)

                    # Create the vals to create the pos receivables that will balance the pos receivables from invoice payment moves.
                    if order_is_invoiced:
                        if is_split_payment:
                            split_inv_payment_receivable_lines[payment] |= payment.account_move_id.line_ids.filtered(lambda line: line.account_id == pos_receivable_account)
                            split_invoice_receivables[payment] = self._update_amounts(split_invoice_receivables[payment], {'amount': payment.amount}, order.date_order)
                        else:
                            combine_inv_payment_receivable_lines[payment_method] |= payment.account_move_id.line_ids.filtered(lambda line: line.account_id == pos_receivable_account)
                            print("combine_invoice_receivables",combine_invoice_receivables[payment_method],{'amount': payment.amount},order.date_order)
                            combine_invoice_receivables[payment_method] = self._update_amounts(combine_invoice_receivables[payment_method], {'amount': payment.amount}, order.date_order)

                    else:
                        # Create global invoice lines
                        for order_line in order.lines:
                            invoice_line = order._prepare_invoice_line(order_line)
                            fiscal_position = self.move_id.fiscal_position_id
                            accounts = order_line.product_id.product_tmpl_id\
                                .get_product_accounts(fiscal_pos=fiscal_position)
                            name = 'Ticket: ' + \
                                order.pos_reference + ' | ' + invoice_line.get('name'),
                            invoice_line.update({
                                'move_id': self.move_id.id,
                                'exclude_from_invoice_tab': False,
                                'account_id': accounts['income'].id,
                                'name': name,
                                'credit': order_line.price_subtotal,
                            })
                            invoice_lines.append(invoice_line)
                            total_paid_orders += order_line.price_subtotal_incl
                        order.partner_id._increase_rank('customer_rank')

                # If pay_later, we create the receivable lines.
                #   if split, with partner
                #   Otherwise, it's aggregated (combined)
                # But only do if order is *not* invoiced because no account move is created for pay later invoice payments.
                if payment_type == 'pay_later' and not order_is_invoiced:
                    if is_split_payment:
                        split_receivables_pay_later[payment] = self._update_amounts(split_receivables_pay_later[payment], {'amount': amount}, date)
                    elif not is_split_payment:
                        combine_receivables_pay_later[payment_method] = self._update_amounts(combine_receivables_pay_later[payment_method], {'amount': amount}, date)

            if not order_is_invoiced:
                order_taxes = defaultdict(tax_amounts)
                for order_line in order.lines:
                    line = self._prepare_line(order_line)
                    # Combine sales/refund lines
                    sale_key = (
                        # account
                        line['income_account_id'],
                        # sign
                        -1 if line['amount'] < 0 else 1,
                        # for taxes
                        tuple((tax['id'], tax['account_id'], tax['tax_repartition_line_id']) for tax in line['taxes']),
                        line['base_tags'],
                    )
                    sales[sale_key] = self._update_amounts(sales[sale_key], {'amount': line['amount']}, line['date_order'])
                    # Combine tax lines
                    for tax in line['taxes']:
                        tax_key = (tax['account_id'] or line['income_account_id'], tax['tax_repartition_line_id'], tax['id'], tuple(tax['tag_ids']))
                        order_taxes[tax_key] = self._update_amounts(
                            order_taxes[tax_key],
                            {'amount': tax['amount'], 'base_amount': tax['base']},
                            tax['date_order'],
                            round=not rounded_globally
                        )
                for tax_key, amounts in order_taxes.items():
                    if rounded_globally:
                        amounts = self._round_amounts(amounts)
                    for amount_key, amount in amounts.items():
                        taxes[tax_key][amount_key] += amount

                if self.company_id.anglo_saxon_accounting and order.picking_ids.ids:
                    # Combine stock lines
                    stock_moves = self.env['stock.move'].sudo().search([
                        ('picking_id', 'in', order.picking_ids.ids),
                        ('company_id.anglo_saxon_accounting', '=', True),
                        ('product_id.categ_id.property_valuation', '=', 'real_time')
                    ])
                    for move in stock_moves:
                        exp_key = move.product_id._get_product_accounts()['expense']
                        out_key = move.product_id.categ_id.property_stock_account_output_categ_id
                        amount = -sum(move.sudo().stock_valuation_layer_ids.mapped('value'))
                        stock_expense[exp_key] = self._update_amounts(stock_expense[exp_key], {'amount': amount}, move.picking_id.date, force_company_currency=True)
                        if move.location_id.usage == 'customer':
                            stock_return[out_key] = self._update_amounts(stock_return[out_key], {'amount': amount}, move.picking_id.date, force_company_currency=True)
                        else:
                            stock_output[out_key] = self._update_amounts(stock_output[out_key], {'amount': amount}, move.picking_id.date, force_company_currency=True)

                if self.config_id.cash_rounding:
                    diff = order.amount_paid - order.amount_total
                    rounding_difference = self._update_amounts(rounding_difference, {'amount': diff}, order.date_order)

                # Increasing current partner's customer_rank
                partners = (order.partner_id | order.partner_id.commercial_partner_id)
                partners._increase_rank('customer_rank')

        if self.company_id.anglo_saxon_accounting:
            global_session_pickings = self.picking_ids.filtered(lambda p: not p.pos_order_id)
            if global_session_pickings:
                stock_moves = self.env['stock.move'].sudo().search([
                    ('picking_id', 'in', global_session_pickings.ids),
                    ('company_id.anglo_saxon_accounting', '=', True),
                    ('product_id.categ_id.property_valuation', '=', 'real_time'),
                ])
                for move in stock_moves:
                    exp_key = move.product_id._get_product_accounts()['expense']
                    out_key = move.product_id.categ_id.property_stock_account_output_categ_id
                    amount = -sum(move.stock_valuation_layer_ids.mapped('value'))
                    stock_expense[exp_key] = self._update_amounts(stock_expense[exp_key], {'amount': amount}, move.picking_id.date, force_company_currency=True)
                    if move.location_id.usage == 'customer':
                        stock_return[out_key] = self._update_amounts(stock_return[out_key], {'amount': amount}, move.picking_id.date, force_company_currency=True)
                    else:
                        stock_output[out_key] = self._update_amounts(stock_output[out_key], {'amount': amount}, move.picking_id.date, force_company_currency=True)
        MoveLine = self.env['account.move.line'].with_context(check_move_validity=False)
        data.update({
            'taxes':                               taxes,
            'sales':                               sales,
            'stock_expense':                       stock_expense,
            'split_receivables_bank':              split_receivables_bank,
            'combine_receivables_bank':            combine_receivables_bank,
            'split_receivables_cash':              split_receivables_cash,
            'combine_receivables_cash':            combine_receivables_cash,
            'combine_invoice_receivables':         combine_invoice_receivables,
            'split_receivables_pay_later':         split_receivables_pay_later,
            'combine_receivables_pay_later':       combine_receivables_pay_later,
            'stock_return':                        stock_return,
            'stock_output':                        stock_output,
            'combine_inv_payment_receivable_lines': combine_inv_payment_receivable_lines,
            'rounding_difference':                 rounding_difference,
            'MoveLine':                            MoveLine,
            'invoice_lines': invoice_lines,
            'total_paid_orders': total_paid_orders,
            'split_invoice_receivables': split_invoice_receivables,
            'split_inv_payment_receivable_lines': split_inv_payment_receivable_lines,
        })
        return data

    def _validate_session(self,balancing_account=False, amount_to_balance=0, bank_payment_method_diffs=None):
        if self.config_id.create_global_invoice \
                and self.config_id.global_invoice_method == 'manual':
            journal = self.config_id.journal_id
            self.ensure_one()
            self._check_if_no_draft_orders()
            account_move = self.env['account.move'].search([
                    ('journal_id', '=',  journal.id),
                    ('date', '=', fields.Date.context_today(self)),
                    ('ref','=', ''),])
            print("_validate_session",account_move.name)
            if account_move.line_ids:
                account_move.write({'payment_state': 'in_payment'})
            if self.move_id.line_ids:
                # Set the uninvoiced orders' state to 'done'
                self.env['pos.order'].search([('session_id', '=', self.id), ('state', '=', 'paid')]).write({'state': 'done'})
            self.write({'state': 'closed'})
            return True
        else:
            res = super(PosSession, self)._validate_session()
            if self.global_invoice_id \
                    and self.config_id.create_global_invoice \
                    and self.config_id.global_invoice_method == 'automatic':
                    
                if self.global_invoice_id.invoice_line_ids:
                    self.write({'has_global_invoice': True})
                else:
                    self.global_invoice_id.unlink()
            return res

    def create_manual_global_invoice(self, records):
        pos_journal = False
        global_customer = False
        # Check if all pos config have the same configuration
        for record in records:
            if not len(record.order_ids):
                raise UserError(
                    _('The session number %s has no orders, please remove it from the '
                      'invoicing list') % (record.name))
            if record.has_global_invoice:
                raise UserError(
                    _('The session number %s has global invoice, please remove it from '
                      'the invoicing list') % (record.name))
            if record.state != 'closed':
                raise UserError(
                    _('The session number %s is not closed, please close this session '
                      'or remove it from the invoicing list') % (record.name))
            if record.move_id:
                raise UserError(
                    _('The session number %s has account move, please remove it from '
                      'the invoicing list') % (record.name))
            if not pos_journal:
                pos_journal = record.config_id.journal_id
            if not global_customer:
                if not record.config_id.global_customer_id:
                    raise UserError(
                        _('Global customer not detected on point of sale %s, please '
                          'configure global customer') % (record.config_id.name))
                global_customer = record.config_id.global_customer_id
            if pos_journal.id != record.config_id.journal_id.id:
                raise UserError(
                    _('All point of sales need to have the same journal to be invoiced '
                      'together, please set the same jornals for all point of sales'))
            if global_customer.id != record.config_id.global_customer_id.id:
                raise UserError(
                    _('All point of sales need to have the same customer global to be '
                      'invoiced together, please set the same customer global for all '
                      'point of sales'))

        account_move = self.env['account.move'].with_context(
            default_journal_id=pos_journal.id).create({
                'journal_id': pos_journal.id,
                'date': fields.Date.context_today(self),
                'ref': '',
            })
        
        global_invoice = self.env['account.move'].with_context(
            default_journal_id=records.config_id.global_journal_id.id).create({
                'journal_id': records.config_id.global_journal_id.id,
                'date': fields.Date.context_today(self),
                'ref': '',
                'partner_id': global_customer.id,
                'move_type': 'out_invoice',
            })
        data = {}
        for pos_session in records:
            ref_account = ((account_move.ref + ' | ') if account_move.ref else '')
            account_move.write({
                'ref': ref_account + pos_session.name
            })
            ref_global = ((global_invoice.ref + ' | ') if global_invoice.ref else '')
            global_invoice.write({
                'ref': ref_global + pos_session.name
            })
            pos_session.write({
                'move_id': account_move.id,
                'global_invoice_id': global_invoice.id,
            })
            data = pos_session._accumulate_amounts_global_invoice(data)
            data = pos_session._create_non_reconciliable_move_lines(data)
            data = pos_session._create_bank_payment_moves(data)
            data = pos_session._create_cash_statement_lines_and_cash_move_lines(data)
            data = pos_session._create_invoice_receivable_lines(data)
            data = pos_session._create_stock_output_lines(data)
            balancing_account= False
            amount_to_balance= 0
            data = pos_session._create_balancing_line(data,balancing_account,amount_to_balance)
            line_receivable = self.env['account.move.line'].search([
                ('account_id', '=', global_customer.property_account_receivable_id.id),
                ('move_id', '=', account_move.id),
                ('name', 'like', _('Sales:'))
            ])  
            if account_move.line_ids:
                print("account_move 405",account_move.name, account_move.id, account_move.line_ids)
                lines = pos_session._line_correction_amounts_global_invoice(account_move.line_ids)
                self.env['account.move.line'].with_context(check_move_validity=False).create(lines)
                self.env['account.move.line'].search([('move_id', '=', account_move.id), ('check_global_invoice', '=', False)]).unlink()
                account_move._post()
            data = pos_session._reconcile_account_move_lines(data)
            print("total_paid_orders",data.get('total_paid_orders'))
            print("data.get('invoice_lines')",data.get('invoice_lines'))
            global_invoice.write({
                'invoice_line_ids': [(0, None, invoice_line) for invoice_line in
                                     data.get('invoice_lines')],
            }) 
        if global_invoice.line_ids:
            global_invoice._post()   
            if global_invoice.line_ids and global_invoice.state == 'posted':
                move_lines = self.env['account.move.line'].search([
                ('move_id', 'in', (global_invoice.id, account_move.id)),
                ('account_id', '=', pos_session.config_id.global_customer_id.property_account_receivable_id.id),
                ('name', '!=', 'From invoiced orders')
                ]).reconcile()
                # print("move_lines",move_lines)
                # for line in move_lines:
                #     print("line",line)
                #     line.reconcile()
             
        records.write({'has_global_invoice': True})
        

        return {
            'name': _('Global invoice'),
            'view_mode': 'form',
            'res_model': 'account.move',
            'type': 'ir.actions.act_window',
            'res_id': global_invoice.id,
        }
    def _line_correction_amounts_global_invoice(self,lines):
        for rec in self:
            if lines:
                vals = []
                for l in lines:
                    if l.debit > 0:
                        l.write({'check_global_invoice':True})
                        vals.append({
                        'move_id':l.move_id.id,
                        'account_id':self.config_id.global_customer_id.property_account_receivable_id.id,
                        'partner_id':self.config_id.global_customer_id.id,
                        'name':  _('Difference at closing PoS session'),
                        'credit':l.debit,
                        'check_global_invoice': True,
                        })
                return vals



    def _prepare_balancing_line_vals(self, imbalance_amount, move):
        account = self._get_balancing_account()
        if not self.config_id.global_customer_id:
            print("_prepare_balancing_line_vals if not self.config_id.global_customer_id:")
            partial_vals = {
                'name': _('Difference at closing PoS session'),
                'account_id': account.id,
                'move_id': move.id,
                'partner_id': False,
            }
        if self.config_id.global_customer_id:
            print("_prepare_balancing_line_vals self.config_id.global_customer_id")
            partial_vals = {
                'name': _('Difference at closing PoS session'),
                'account_id': account.id,
                'move_id': move.id,
                'partner_id': self.config_id.global_customer_id.id,
            }
        # `imbalance_amount` is already in terms of company currency so it is the amount_converted
        # param when calling `_credit_amounts`. amount param will be the converted value of
        # `imbalance_amount` from company currency to the session currency.
        imbalance_amount_session = 0
        if (not self.is_in_company_currency):
            imbalance_amount_session = self.company_id.currency_id._convert(imbalance_amount, self.currency_id, self.company_id, fields.Date.context_today(self))
        return self._credit_amounts(partial_vals, imbalance_amount_session, imbalance_amount)