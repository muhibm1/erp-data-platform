-- Every dollar still owed shows up in exactly one aging bucket.
select r.customer_key, r.open, a.total_open
from {{ ref('revenue_by_customer') }} r
left join {{ ref('receivables_aging') }} a using (customer_key)
where abs(r.open - coalesce(a.total_open, 0)) > 0.01
   or abs(coalesce(a.total_open, 0) - (coalesce(a.not_yet_due, 0) + coalesce(a.late_1_30, 0)
       + coalesce(a.late_31_60, 0) + coalesce(a.late_61_90, 0) + coalesce(a.late_over_90, 0))) > 0.01
