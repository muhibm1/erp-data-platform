"""What we pull from Odoo, and why each field is there.

Field lists are a floor, not a ceiling: extraction also keeps any custom field
(x_ prefix) a client added, so a new field never breaks a load.

Some fields are computed, not stored (sale.order.invoice_ids, and on
account.payment the name, date, state and reconciled_invoice_ids). Odoo
returns them on read, but changing them does not always move the record's
write_date, so a link can arrive one load late. The checks in dbt catch that.
"""

SOURCES = {
    # CRM: who we sold to, and the deal before it became an order.
    "res.partner": ["name", "email", "is_company", "commercial_partner_id",
                    "country_id", "active", "create_date"],
    "crm.stage": ["name", "is_won", "sequence"],
    "crm.lead": ["name", "type", "partner_id", "stage_id", "expected_revenue",
                 "probability", "date_closed", "active", "create_date"],
    # Sales: the order a won deal turns into.
    "sale.order": ["name", "partner_id", "opportunity_id", "state", "date_order",
                   "amount_total", "currency_id", "invoice_ids"],
    # Accounting: what was billed and what was paid.
    "account.move": ["name", "move_type", "state", "partner_id", "invoice_date",
                     "invoice_date_due", "amount_total", "amount_total_signed",
                     "amount_residual", "amount_residual_signed", "currency_id",
                     "payment_state", "invoice_origin"],
    "account.payment": ["name", "partner_id", "amount", "amount_company_currency_signed",
                        "currency_id", "date", "state", "payment_type", "partner_type",
                        "reconciled_invoice_ids"],
    "res.currency": ["name", "symbol", "active"],
}

# Only customer documents matter for deal to cash.
DOMAINS = {
    "account.move": [("move_type", "in", ["out_invoice", "out_refund"])],
    "account.payment": [("partner_type", "=", "customer")],
    "crm.lead": [("active", "in", [True, False])],
    "res.partner": [("active", "in", [True, False])],
}


def table_name(model: str) -> str:
    """raw table for an Odoo model: crm.lead -> raw.crm_lead."""
    return model.replace(".", "_")
