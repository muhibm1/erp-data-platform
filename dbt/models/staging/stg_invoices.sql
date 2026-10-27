-- Customer invoices and credit notes. Signed amounts are in the company
-- currency (negative for credit notes), which is what every total uses.
with m as ({{ latest('account_move', 'account.move') }})
select
    odoo_id as invoice_id,
    payload->>'name' as invoice_ref,
    payload->>'move_type' as move_type,
    payload->>'state' as state,
    {{ m2o_id('partner_id') }} as partner_id,
    ({{ txt('invoice_date') }})::date as invoice_date,
    ({{ txt('invoice_date_due') }})::date as due_date,
    {{ m2o_name('currency_id') }} as currency,
    (payload->>'amount_total')::numeric as amount_in_invoice_currency,
    (payload->>'amount_total_signed')::numeric as amount,
    (payload->>'amount_residual_signed')::numeric as amount_open,
    payload->>'payment_state' as payment_state,
    {{ txt('invoice_origin') }} as invoice_origin,
    write_date as source_version
from m
