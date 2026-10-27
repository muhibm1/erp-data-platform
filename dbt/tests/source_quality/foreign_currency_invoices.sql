-- Source problem 3: invoices not in USD. Totals use Odoo's USD amount at the
-- invoice date's rate; this lists them so the rate can be checked.
{{ config(severity='warn') }}
select invoice_id, invoice_ref, currency, amount_in_invoice_currency, amount as amount_usd
from {{ ref('int_invoices') }}
where currency <> '{{ var("company_currency") }}'
