-- What each customer was billed and has paid, in USD, with the invoices and
-- payments behind every figure.
with inv as (select * from {{ ref('int_invoices') }}),
pay as (
    select c.customer_key, p.payment_id
    from {{ ref('stg_payments') }} p
    join {{ ref('int_customers') }} c on c.partner_id = p.partner_id
    where p.state = 'posted' and p.payment_type = 'inbound'
)
select
    inv.customer_key,
    max(inv.customer_name) as customer_name,
    sum(inv.amount) as invoiced,
    sum(inv.amount_paid) as paid,
    sum(inv.amount_open) as open,
    count(*) as invoice_count,
    array_agg(inv.invoice_id order by inv.invoice_id) as source_invoice_ids,
    (select array_agg(payment_id order by payment_id) from pay where pay.customer_key = inv.customer_key) as source_payment_ids
from inv
group by inv.customer_key
