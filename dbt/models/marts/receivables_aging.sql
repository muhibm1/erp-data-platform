-- Money still owed, per customer, by how late it is as of the as_of date.
with open_inv as (select * from {{ ref('int_invoices') }} where amount_open <> 0)
select
    customer_key,
    max(customer_name) as customer_name,
    sum(amount_open) filter (where days_past_due = 0) as not_yet_due,
    sum(amount_open) filter (where days_past_due between 1 and 30) as late_1_30,
    sum(amount_open) filter (where days_past_due between 31 and 60) as late_31_60,
    sum(amount_open) filter (where days_past_due between 61 and 90) as late_61_90,
    sum(amount_open) filter (where days_past_due > 90) as late_over_90,
    sum(amount_open) as total_open,
    array_agg(invoice_id order by invoice_id) as source_invoice_ids
from open_inv
group by customer_key
