-- Source problem 1: the same company exists twice in Odoo.
-- The models merge them (int_customers); this check lists every merge so a
-- person can confirm it and fix the record in Odoo.
{{ config(severity='warn') }}
select customer_key, customer_name, array_agg(partner_id order by partner_id) as odoo_partner_ids
from {{ ref('int_customers') }} c
join {{ ref('stg_partners') }} p using (partner_id)
where p.partner_id = coalesce(p.commercial_partner_id, p.partner_id)
group by 1, 2
having count(*) > 1
