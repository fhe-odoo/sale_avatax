from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class AccountInvoice(models.Model):
    _inherit = "account.invoice"

    amount_tax_expense = fields.Float(
        string="Tax Expense",
        compute="_compute_amount_tax_expense",
        # TODO: should this be compute="_compute_amount" ?
    )

    @api.depends("invoice_line_ids.tax_expense")
    def _compute_amount_tax_expense(self):
        """
        Compute the Tax Expense amount
        """
        for inv in self:
            round_curr = inv.currency_id.round
            inv.amount_tax_expense = round_curr(
                sum(line.tax_expense for line in inv.invoice_line_ids)
            )

    def get_taxes_values(self, contact_avatax=False, commit_avatax=False):
        """
        Extends the standard method reponsible for computing taxes.
        Returns a dict with the taxes values, ready to be used to create tax_line_ids.
        """
        tax_grouped = super(AccountInvoice, self).get_taxes_values()
        avatax_config = self.company_id.get_avatax_config_company()
        has_ava_use = any(
            x.tax_id.is_avatax and x.tax_id.is_expensed_tax for x in self.tax_line_ids
        )
        if self.type in ["out_invoice", "out_refund"] and avatax_config and has_ava_use:
            Tax = self.env["account.tax"]
            for line in self.invoice_line_ids:
                tax_ids = line.invoice_line_tax_ids.filtered("is_expensed_tax")
                if not line.account_id or line.display_type or not tax_ids:
                    continue
                price_unit = line._get_tax_price_unit()
                taxes = tax_ids.compute_all(
                    price_unit,
                    self.currency_id,
                    line.quantity,
                    line.product_id,
                    self.partner_id,
                )["taxes"]
                for tax in taxes:
                    val = self._prepare_tax_line_vals(line, tax)
                    key = Tax.browse(tax["id"]).get_grouping_key(val)
                    tax_grouped_val = tax_grouped[key]
                    tax_grouped_val.setdefault("amount_tax_expense", 0)
                    tax_grouped_val["amount_tax_expense"] += tax.get(
                        "amount_expense", 0
                    )
        return tax_grouped

    def tax_line_move_line_get(self):
        res = super().tax_line_move_line_get() or []
        return res  #FIXME
        for tax_line in sorted(self.tax_line_ids, key=lambda x: -x.sequence):
            if tax_line.amount_tax_expense and tax_line.tax_id.expense_account_id:
                # Tax Payable move
                res.append(tax_line._prepare_tax_expense_move_vals(sign=+1))
                # Tax Expense move
                res.append(tax_line._prepare_tax_expense_move_vals(sign=-1))
        return res

    @api.onchange("fiscal_position_id")
    def _onchange_fiscal_position_set_taxes(self):
        """
        Recompute taxes if the fiscal position is changed on the Invoice.
        """
        for line in self.mapped("invoice_line_ids"):
            line.tax_amt = 0
            line.tax_amt_expense = 0
            # _set_taxes() reapplies taxes based on Product and Fiscal position
            # It resets the Unit Price to the List Price, but we don't want that
            price = line.price_unit
            line._set_taxes()
            line.price_unit = price

    def _get_avatax_doc_type(self, commit=False):
        doc_type = super()._get_avatax_doc_type(commit=commit)
        taxes = self.invoice_line_ids.mapped("invoice_line_tax_ids").filtered(
            "is_avatax"
        )
        taxes_expense = taxes.filtered("is_expensed_tax")
        if taxes_expense:
            # Nice to have:
            # if not len(taxes_expense) != len(taxes):
            # warn can't mix tax types
            doc_type = doc_type.replace("Sales", "Purchase")
        return doc_type

    def _compute_amount(self):
        """
        Compute the Tax Expense field
        Needs to be done on the same method computing the tax amount.
        """
        super()._compute_amount()
        for inv in self:
            inv.amount_tax_expense = sum(
                line.tax_amt_expense or line.tax_expense
                for line in inv.invoice_line_ids
            )

    def _avatax_compute_tax(self, commit=False):
        tax_result = super()._avatax_compute_tax(commit=commit)
        doc_type = self._get_avatax_doc_type()
        if self.amount_tax_expense and doc_type.startswith("Purchase"):
            self.avatax_amount = 0
            for line in self.invoice_line_ids:
                line.tax_amt_expense = line.tax_amt
                line.tax_amt = 0
        else:
            self.amount_tax_expense = 0
            for line in self.invoice_line_ids:
                line.tax_amt_expense = 0
        return tax_result

    def action_move_create(self):
        """
        Ensure the expense account moves are generated.
        Apparently random cases were found where they were not generated.
        This validation detects and prevents that from happening,
        and should help understand the circumstances cauing that,
        """
        res = super().action_move_create()
        for invoice in self:
            expense_accounts_config = invoice.invoice_line_ids.mapped(
                "invoice_line_tax_ids.expense_account_id"
            )
            expense_accounts_used = invoice.move_id.line_ids.mapped("account_id")
            for account in expense_accounts_config:
                if account not in expense_accounts_used:
                    raise ValidationError(
                        _("Invoice %s is missing expected tax accounting lines for %s")
                        % (invoice.number, account.display_name)
                    )
        return res


class AccountInvoiceTax(models.Model):

    _inherit = "account.invoice.tax"

    amount_tax_expense = fields.Monetary(string="Tax Expense")

    def _prepare_tax_expense_move_vals(self, sign=1):
        self.ensure_one()
        tax_line = self
        account = (
            tax_line.account_id if sign == 1 else tax_line.tax_id.expense_account_id
        )
        analytic_tag_ids = [
            (4, analytic_tag.id, None) for analytic_tag in tax_line.analytic_tag_ids
        ]
        value = {
            "invoice_tax_line_id": tax_line.id,
            "tax_line_id": tax_line.tax_id.id,
            "type": "tax",
            "name": tax_line.name,
            "price_unit": tax_line.amount_tax_expense,
            "quantity": 1 * sign,
            "price": tax_line.amount_tax_expense * sign,
            "account_id": account.id,
            "account_analytic_id": tax_line.account_analytic_id.id,
            "analytic_tag_ids": analytic_tag_ids,
            "invoice_id": tax_line.invoice_id.id,
        }
        return value


class AccountInvoiceLine(models.Model):
    _inherit = "account.invoice.line"

    tax_amt_expense = fields.Float("Avalara Tax Expense")

    tax_expense = fields.Monetary(compute="_compute_price", help="Tax expense amount",)
    tax_total = fields.Monetary(
        compute="_compute_price", help="Total tax amount, collected plus expensed",
    )

    @api.onchange(
        "price_unit", "discount", "invoice_line_tax_ids", "quantity", "purchase_price"
    )
    def onchange_reset_avatax_amount(self):
        super().onchange_reset_avatax_amount()
        for line in self:
            line.tax_amt_expense = 0

    def _get_tax_price_unit(self):
        """
        Returns the Base Amount to use for Expensed Tax.
        Use Tax is computed based on purchase price,
        that should be stored in the "purchase_price" field.
        """
        res = super()._get_tax_price_unit()
        if any(x.is_expensed_tax for x in self.invoice_line_tax_ids):
            price_unit = self.purchase_price or self.price_unit
            res = price_unit * (1 - (self.discount or 0.0) / 100.0)
        return res

    @api.depends(
        "price_unit",
        "discount",
        "invoice_line_tax_ids",
        "quantity",
        "product_id",
        "invoice_id.partner_id",
        "invoice_id.currency_id",
        "invoice_id.company_id",
        "invoice_id.date_invoice",
        "invoice_id.date",
        "purchase_price",  # added
        "tax_amt",  # added
        "tax_amt_expense",  # added
    )
    def _compute_price(self):
        """
        Compute the tax_expense field.
        Needs to be done on the same method computing the tax amount.
        """
        super()._compute_price()
        for line in self:
            has_expensed_tax = any(x.is_expensed_tax for x in line.invoice_line_tax_ids)
            if has_expensed_tax:
                currency = line.invoice_id.currency_id or None
                price = line._get_tax_price_unit()
                tax_ids = line.invoice_line_tax_ids.with_context(avatax_line=line)
                taxes = tax_ids.compute_all(
                    price,
                    currency,
                    line.quantity,
                    product=line.product_id,
                    partner=line.invoice_id.partner_id,
                )
                line.tax_expense = taxes.get("total_expense", 0) if taxes else 0
            line.tax_total = line.price_tax + line.tax_expense

    @api.depends("purchase_price", "price_subtotal", "tax_expense")
    def _compute_margin(self):
        super()._compute_margin()
        applicable = self.filtered(
            lambda x: x.invoice_id and x.invoice_id.type[:2] != "in"
        )
        for line in applicable:
            sign = line.invoice_id.type in ["in_refund", "out_refund"] and -1 or 1
            line.margin = line.margin - line.tax_expense
            line.margin_signed = line.margin * sign
            line.margin_percent = (
                line.margin / line.price_subtotal * 100.0
                if line.price_subtotal
                else 0.0
            )
