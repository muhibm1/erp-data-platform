{# Odoo sends a many2one as [id, "display name"], or false when empty. #}
{% macro m2o_id(col) -%} (case when jsonb_typeof(payload->'{{ col }}') = 'array' then (payload->'{{ col }}'->>0)::int end) {%- endmacro %}
{% macro m2o_name(col) -%} (case when jsonb_typeof(payload->'{{ col }}') = 'array' then payload->'{{ col }}'->>1 end) {%- endmacro %}
{# Odoo sends an empty char or date as false, not null. #}
{% macro txt(col) -%} nullif(nullif(payload->>'{{ col }}', 'false'), '') {%- endmacro %}

{# The newest version of each record that has not been deleted in Odoo. #}
{% macro latest(table, model) -%}
  select distinct on (r.odoo_id) r.*
  from {{ source('odoo', table) }} r
  where not exists (select 1 from {{ source('odoo', 'deleted') }} d
                    where d.model = '{{ model }}' and d.odoo_id = r.odoo_id)
  order by r.odoo_id, r.write_date desc
{%- endmacro %}
