-- Source problem 4: a deal marked won in the CRM with no confirmed order.
-- Either the order was never entered, or the deal should not be won.
{{ config(severity='warn') }}
select deal_id, deal_name, customer_name, expected_revenue
from {{ ref('deal_to_cash') }}
where status = 'won, no order'
