-- Every won deal, followed through order, invoice and payment.
with won as (select * from {{ ref('stg_deals') }} where is_won),
orders as (
    select deal_id, array_agg(order_id order by order_id) as order_ids, sum(amount_total) as ordered
    from {{ ref('stg_orders') }} where state in ('sale', 'done') group by deal_id
),
inv as (
    select deal_id, array_agg(invoice_id order by invoice_id) as invoice_ids,
        sum(amount) as invoiced, sum(amount_paid) as paid, sum(amount_open) as open
    from {{ ref('int_invoices') }} where deal_id is not null group by deal_id
)
select
    w.deal_id, w.deal_name, c.customer_key, c.customer_name,
    w.expected_revenue,
    coalesce(o.ordered, 0) as ordered,
    coalesce(i.invoiced, 0) as invoiced,
    coalesce(i.paid, 0) as paid,
    coalesce(i.open, 0) as open,
    case
        when o.deal_id is null then 'won, no order'
        when i.deal_id is null then 'ordered, not invoiced'
        when i.open = 0 then 'paid'
        when i.paid > 0 then 'partly paid'
        else 'invoiced, unpaid'
    end as status,
    coalesce(o.order_ids, '{}') as source_order_ids,
    coalesce(i.invoice_ids, '{}') as source_invoice_ids
from won w
left join {{ ref('int_customers') }} c on c.partner_id = w.partner_id
left join orders o on o.deal_id = w.deal_id
left join inv i on i.deal_id = w.deal_id
