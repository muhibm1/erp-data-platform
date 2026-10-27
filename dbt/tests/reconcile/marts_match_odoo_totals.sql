-- The finance tables add up to the same billed total as the source invoices.
with src as (
    select coalesce(sum(amount), 0) as billed from {{ ref('stg_invoices') }}
    where state = 'posted' and move_type in ('out_invoice', 'out_refund')
),
mart as (select coalesce(sum(invoiced), 0) as billed from {{ ref('revenue_by_customer') }})
select src.billed, mart.billed from src, mart where abs(src.billed - mart.billed) > 0.01
