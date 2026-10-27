-- What the invoices say was paid equals the posted customer payments, per customer.
-- A gap means a payment is not matched to an invoice, or an exchange difference.
{{ config(severity='warn') }}
with paid_by_invoice as (
    select customer_key, sum(amount_paid) as paid from {{ ref('int_invoices') }} group by 1
),
paid_by_payment as (
    select c.customer_key, sum(p.amount) as paid
    from {{ ref('stg_payments') }} p
    join {{ ref('int_customers') }} c on c.partner_id = p.partner_id
    where p.state = 'posted' and p.payment_type = 'inbound'
    group by 1
)
select coalesce(i.customer_key, p.customer_key) as customer_key, i.paid as per_invoices, p.paid as per_payments
from paid_by_invoice i full join paid_by_payment p using (customer_key)
where abs(coalesce(i.paid, 0) - coalesce(p.paid, 0)) > 0.01
