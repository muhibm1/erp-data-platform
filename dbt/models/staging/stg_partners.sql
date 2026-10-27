with p as ({{ latest('res_partner', 'res.partner') }})
select
    odoo_id as partner_id,
    payload->>'name' as partner_name,
    lower({{ txt('email') }}) as email,
    (payload->>'is_company')::boolean as is_company,
    {{ m2o_id('commercial_partner_id') }} as commercial_partner_id,
    {{ m2o_name('country_id') }} as country,
    write_date as source_version
from p
