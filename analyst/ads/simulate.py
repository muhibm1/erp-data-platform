"""A simulated app-store search ads account, to show the analyst on a second dataset.

Nothing here comes from a real ad platform. It is a seeded, generic account in
the shape such reporting usually takes: apps advertise on search keywords in
several countries, and every day each keyword has impressions (its ad was
shown), taps (someone tapped it), installs (the tap led to a download) and
spend (what the taps cost).

Three things make it a fair test of the agent, not a lookup:

- Each campaign spends in its country's currency. Spend in US dollars needs the
  daily exchange rate table.
- Ratios have a right and a wrong way to roll up. Cost per install over a month
  is total spend over total installs, not the average of daily ratios.
- Audience breakdowns (age band and gender) are privacy-sensitive. User-level
  taps sit in a private schema the agent's role cannot read, and the query tool
  blanks any result row built from fewer than 50 installs.

  python -m analyst.ads.simulate          # builds database "adsdemo" (drops it first)
"""

import argparse
import datetime as dt
import hashlib
import math
import random

import psycopg

DAYS = 60
LAST_DAY = dt.date(2026, 10, 26)
FIRST_DAY = LAST_DAY - dt.timedelta(days=DAYS - 1)
READER = "ads_reader"

APPS = [
    ("Trailhead", "Hiking maps"), ("Ledgerly", "Personal budgeting"), ("Sprout", "Plant care"),
]
COUNTRIES = {"US": ("USD", 1.0), "GB": ("GBP", 0.79), "DE": ("EUR", 0.92), "JP": ("JPY", 151.0)}
CAMPAIGNS = [
    # (id, app, country, name, daily budget in local currency, status, started)
    (1, "Trailhead", "US", "Trailhead - US - Search", 1000, "running", FIRST_DAY),
    (2, "Trailhead", "GB", "Trailhead - GB - Search", 300, "running", FIRST_DAY),
    (3, "Ledgerly", "US", "Ledgerly - US - Search", 1100, "running", FIRST_DAY),
    (4, "Ledgerly", "DE", "Ledgerly - DE - Search", 300, "paused", FIRST_DAY),
    (5, "Sprout", "US", "Sprout - US - Search", 600, "running", FIRST_DAY + dt.timedelta(days=14)),
    (6, "Sprout", "JP", "Sprout - JP - Search", 40000, "running", FIRST_DAY),
]
AD_GROUPS = ["Brand", "Category", "Competitor"]
KEYWORDS = {
    "Trailhead": {"Brand": ["trailhead", "trailhead app", "trailhead maps"],
                  "Category": ["hiking app", "trail maps", "offline maps", "hiking trails near me"],
                  "Competitor": ["alltrails", "gaia gps", "komoot"]},
    "Ledgerly": {"Brand": ["ledgerly", "ledgerly budget"],
                 "Category": ["budget app", "expense tracker", "money manager", "budget planner"],
                 "Competitor": ["ynab", "mint budget", "copilot money"]},
    "Sprout": {"Brand": ["sprout", "sprout plants"],
               "Category": ["plant care", "plant identifier", "watering reminder"],
               "Competitor": ["planta", "greg plant app"]},
}
# Funnel by ad group: (tap-through rate, conversion rate, cost per tap in USD)
FUNNEL = {"Brand": (0.11, 0.62, 0.45), "Category": (0.045, 0.38, 1.35), "Competitor": (0.03, 0.22, 1.9)}
AGE_BANDS = ["18-24", "25-34", "35-44", "45-54", "55+"]
AGE_SHARE = [0.18, 0.31, 0.24, 0.16, 0.11]
EXPERIMENTS = [
    # (id, campaign, name, started, days, conversion rate A, conversion rate B)
    (1, 3, "Product page: new screenshots", LAST_DAY - dt.timedelta(days=27), 28, 0.40, 0.44),
    (2, 1, "Product page: shorter subtitle", LAST_DAY - dt.timedelta(days=20), 21, 0.43, 0.43),
]


def _poisson(rng: random.Random, lam: float) -> int:
    if lam > 50:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _binom(rng: random.Random, n: int, p: float) -> int:
    if n > 60:
        return max(0, min(n, int(round(rng.gauss(n * p, math.sqrt(n * p * (1 - p)))))))
    return sum(rng.random() < p for _ in range(n))


def build(admin_dsn: str, db: str = "adsdemo", seed: int = 2026) -> dict:
    rng = random.Random(seed)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"drop database if exists {db} with (force)")
        admin.execute(f"create database {db}")
    dsn = admin_dsn.replace("dbname=postgres", f"dbname={db}") if "dbname=" in admin_dsn else f"{admin_dsn} dbname={db}"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(open(__file__.replace("simulate.py", "schema.sql")).read())
        rates = {}
        for i in range(DAYS):
            day = FIRST_DAY + dt.timedelta(days=i)
            for code, (cur, base) in COUNTRIES.items():
                rate = round(base * (1 + rng.gauss(0, 0.004)), 4) if cur != "USD" else 1.0
                rates[(day, cur)] = rate
                c.execute("insert into reporting.fx_rates values (%s, %s, %s)", (day, cur, rate))
        kid = 0
        stats = audience = taps_rows = 0
        for cid, app, country, name, budget, status, started in CAMPAIGNS:
            cur = COUNTRIES[country][0]
            c.execute("insert into reporting.campaigns values (%s,%s,%s,%s,%s,%s,%s,%s)",
                      (cid, app, name, country, cur, budget, status, started))
            for g, group in enumerate(AD_GROUPS):
                gid = cid * 10 + g
                c.execute("insert into reporting.ad_groups values (%s,%s,%s)", (gid, cid, group))
                for word in KEYWORDS[app][group]:
                    kid += 1
                    match = "exact" if group == "Brand" or rng.random() < 0.5 else "broad"
                    ttr, cr, cpt = FUNNEL[group]
                    ttr *= rng.uniform(0.7, 1.3)
                    cr *= rng.uniform(0.8, 1.2)
                    cpt *= rng.uniform(0.8, 1.25)
                    volume = rng.uniform(300, 2500) * (0.35 if country != "US" else 1.0)
                    bid = round(cpt * 1.3 * COUNTRIES[country][1], 2)
                    c.execute("insert into reporting.keywords values (%s,%s,%s,%s,%s)", (kid, gid, word, match, bid))
                    for i in range(DAYS):
                        day = FIRST_DAY + dt.timedelta(days=i)
                        if day < started or (status == "paused" and day > LAST_DAY - dt.timedelta(days=12)):
                            continue
                        weekend = 1.15 if day.weekday() >= 5 else 1.0
                        impressions = _poisson(rng, volume * weekend)
                        taps = _binom(rng, impressions, ttr)
                        installs = _binom(rng, taps, cr)
                        spend_usd = taps * cpt * rng.uniform(0.9, 1.1)
                        spend = round(spend_usd * rates[(day, cur)], 2 if cur != "JPY" else 0)
                        c.execute("insert into reporting.keyword_daily values (%s,%s,%s,%s,%s,%s)",
                                  (day, kid, impressions, taps, installs, spend))
                        stats += 1
                        # A small sample of user-level taps lands in the private schema.
                        for t in range(min(taps, 3)):
                            user = hashlib.sha256(f"{seed}-{kid}-{day}-{t}".encode()).hexdigest()[:16]
                            c.execute("insert into events.taps values (%s,%s,%s,%s)", (user, kid, day, t < installs))
                            taps_rows += 1
            # Audience breakdown per campaign and day, from that day's campaign installs.
            daily = c.execute("""select day, sum(impressions), sum(taps), sum(installs) from reporting.keyword_daily kd
                         join reporting.keywords k using (keyword_id) join reporting.ad_groups g using (ad_group_id)
                         where g.campaign_id = %s group by day order by day""", (cid,))
            for day, imp, tp, inst in daily.fetchall():
                shares = [rng.uniform(0.8, 1.2) * s for s in AGE_SHARE]
                total = sum(shares)
                for band, share in zip(AGE_BANDS, shares):
                    female = 0.5 + rng.gauss(0, 0.04)
                    for gender, gshare in (("female", female), ("male", 1 - female)):
                        f = share / total * gshare
                        c.execute("insert into reporting.audience_daily values (%s,%s,%s,%s,%s,%s,%s)",
                                  (day, cid, band, gender, int(imp * f), int(tp * f), int(inst * f)))
                        audience += 1
        for eid, cid, name, started, days, cr_a, cr_b in EXPERIMENTS:
            c.execute("insert into reporting.experiments values (%s,%s,%s,%s,%s)",
                      (eid, cid, name, started, started + dt.timedelta(days=days - 1)))
            for i in range(days):
                day = started + dt.timedelta(days=i)
                for variant, cr in (("A", cr_a), ("B", cr_b)):
                    taps = _poisson(rng, 420)
                    c.execute("insert into reporting.experiment_daily values (%s,%s,%s,%s,%s)",
                              (eid, variant, day, taps, _binom(rng, taps, cr)))
        c.execute(f"""
            do $$ begin
              if not exists (select 1 from pg_roles where rolname = '{READER}') then
                create role {READER} nologin;
              end if;
            end $$;
            grant usage on schema reporting to {READER};
            grant select on all tables in schema reporting to {READER};
            revoke all on schema events from {READER};""")
    return {"database": db, "keywords": kid, "keyword_days": stats, "audience_rows": audience,
            "private_tap_rows": taps_rows, "from": FIRST_DAY.isoformat(), "to": LAST_DAY.isoformat()}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--admin-dsn", default="host=/tmp dbname=postgres user=odoo")
    print(build(p.parse_args().admin_dsn))
