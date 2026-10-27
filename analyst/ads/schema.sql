-- The simulated search ads account. `reporting` is what an analyst may read;
-- `events` holds user-level taps and is never granted to the reader role.
create schema reporting;
create schema events;

create table reporting.campaigns (
    campaign_id int primary key, app_name text, campaign_name text, country text,
    currency text, daily_budget numeric, status text, started_on date);
create table reporting.ad_groups (ad_group_id int primary key, campaign_id int, ad_group_name text);
create table reporting.keywords (keyword_id int primary key, ad_group_id int, keyword text, match_type text, bid numeric);
create table reporting.keyword_daily (
    day date, keyword_id int, impressions int, taps int, installs int, spend numeric,
    primary key (day, keyword_id));
create table reporting.audience_daily (
    day date, campaign_id int, age_band text, gender text, impressions int, taps int, installs int);
create table reporting.fx_rates (day date, currency text, units_per_usd numeric, primary key (day, currency));
create table reporting.experiments (
    experiment_id int primary key, campaign_id int, experiment_name text, started_on date, ended_on date);
create table reporting.experiment_daily (experiment_id int, variant text, day date, taps int, installs int);

create table events.taps (user_hash text, keyword_id int, day date, installed boolean);
