-- Source problem 5: records Odoo no longer has. They are left out of every
-- model; raw keeps their last version for audit.
{{ config(severity='warn') }}
select model, odoo_id, seen_missing_at from {{ source('odoo', 'deleted') }}
