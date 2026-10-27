-- Posted customer invoices and credit notes, with the customer, the order and
-- the deal behind each one, and a source reference down to the exact version.
with orders as (
    select o.order_id, o.deal_id, unnest(o.invoice_ids) as invoice_id from {{ ref('stg_orders') }} o
)
select
    i.invoice_id, i.invoice_ref, i.move_type, i.invoice_date, i.due_date,
    c.customer_key, c.customer_name, i.partner_id,
    i.currency, i.amount_in_invoice_currency, i.amount, i.amount_open,
    i.amount - i.amount_open as amount_paid,
    greatest(0, '{{ var("as_of") }}'::date - i.due_date) as days_past_due,
    o.order_id, o.deal_id,
    'account.move/' || i.invoice_id || '@' || to_char(i.source_version, 'YYYY-MM-DD"T"HH24:MI:SS') as source_ref
from {{ ref('stg_invoices') }} i
join {{ ref('int_customers') }} c on c.partner_id = i.partner_id
left join orders o on o.invoice_id = i.invoice_id
where i.state = 'posted' and i.move_type in ('out_invoice', 'out_refund')
