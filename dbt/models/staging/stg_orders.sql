with o as ({{ latest('sale_order', 'sale.order') }})
select
    odoo_id as order_id,
    payload->>'name' as order_ref,
    {{ m2o_id('partner_id') }} as partner_id,
    {{ m2o_id('opportunity_id') }} as deal_id,
    payload->>'state' as state,
    (payload->>'date_order')::timestamp::date as order_date,
    (payload->>'amount_total')::numeric as amount_total,
    {{ m2o_name('currency_id') }} as currency,
    array(select jsonb_array_elements_text(payload->'invoice_ids')::int) as invoice_ids,
    write_date as source_version
from o
