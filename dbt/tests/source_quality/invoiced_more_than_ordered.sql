-- An order billed for more than it was sold for.
{{ config(severity='warn') }}
select deal_id, deal_name, ordered, invoiced, source_invoice_ids
from {{ ref('deal_to_cash') }}
where invoiced > ordered + 0.01
