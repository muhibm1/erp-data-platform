-- One customer per real-world company. Odoo can hold the same company twice
-- (a second rep, a new spelling, "Inc." added), so partners are grouped by a
-- normalised email or a normalised name, and the oldest record is the key.
with p as (
    select s.*,
        coalesce(s.commercial_partner_id, s.partner_id) as company_id,
        trim(regexp_replace(regexp_replace(lower(s.partner_name), '[^a-z0-9 ]', ' ', 'g'),
             '\m(inc|llc|ltd|co|corp|corporation|company)\M', '', 'g')) as name_norm
    from {{ ref('stg_partners') }} s
),
companies as (
    select partner_id as company_id, partner_name, email,
        regexp_replace(name_norm, '\s+', ' ', 'g') as name_norm
    from p where partner_id = company_id
),
keyed as (
    select c.*,
        least(
            min(company_id) over (partition by name_norm),
            -- A missing email is not a match: Postgres would put every partner
            -- without one in the same window and merge them.
            case when email is null then company_id
                 else min(company_id) over (partition by email) end
        ) as customer_key
    from companies c
)
select
    p.partner_id,
    k.customer_key,
    key_row.partner_name as customer_name,
    count(*) over (partition by k.customer_key) > 1 and p.partner_id = p.company_id
        and k.customer_key <> p.partner_id as merged_into_other
from p
join keyed k on k.company_id = p.company_id
join companies key_row on key_row.company_id = k.customer_key
