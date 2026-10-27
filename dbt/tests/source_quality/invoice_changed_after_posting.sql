-- Source problem 2: an invoice was posted, then reopened and changed.
-- Totals use the newest version; the older posted amount stays in raw.
{{ config(severity='warn') }}
select invoice_id,
    array_agg(amount order by version_no) as posted_amounts,
    array_agg(source_version order by version_no) as versions
from {{ ref('stg_invoice_versions') }}
where state = 'posted'
group by invoice_id
having count(distinct amount) > 1
