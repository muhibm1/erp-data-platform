with l as ({{ latest('crm_lead', 'crm.lead') }}),
stages as ({{ latest('crm_stage', 'crm.stage') }})
select
    l.odoo_id as deal_id,
    l.payload->>'name' as deal_name,
    (case when jsonb_typeof(l.payload->'partner_id') = 'array' then (l.payload->'partner_id'->>0)::int end) as partner_id,
    s.payload->>'name' as stage,
    coalesce((s.payload->>'is_won')::boolean, false) as is_won,
    not (l.payload->>'active')::boolean as is_lost,
    (l.payload->>'expected_revenue')::numeric as expected_revenue,
    (l.payload->>'create_date')::timestamp as created_at,
    (nullif(l.payload->>'date_closed', 'false'))::timestamp::date as closed_on,
    l.write_date as source_version
from l
left join stages s on s.odoo_id = (case when jsonb_typeof(l.payload->'stage_id') = 'array' then (l.payload->'stage_id'->>0)::int end)
where l.payload->>'type' = 'opportunity'
