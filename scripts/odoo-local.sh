#!/usr/bin/env bash
# Runs Odoo 17 Community from source against Postgres, with demo data and the
# CRM, Sales and Invoicing apps. Usage:
#   scripts/odoo-local.sh install   # download Odoo 17 and its Python packages into ./.odoo
#   scripts/odoo-local.sh reset     # drop and recreate the "acme" database, then start the server
#   scripts/odoo-local.sh stop      # stop the server (the drills use this to simulate an outage)
#   scripts/odoo-local.sh start     # start it again
# Needs Python 3.11 and a Postgres 16 server, reached as a role that can create
# databases. Defaults: a socket in /tmp and the role odoo. The standard
# Postgres variables (PGHOST, PGUSER, a password variable) point it elsewhere;
# CI uses them. Odoo's database driver reads them too, so no password is
# written to the config file.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$ROOT/.odoo"
export PGHOST="${PGHOST:-/tmp}" PGUSER="${PGUSER:-odoo}"

start() {
  nohup "$HOME_DIR/venv/bin/odoo" -c "$HOME_DIR/odoo.conf" -d acme --db-filter=acme \
    > "$HOME_DIR/server.log" 2>&1 &
  echo $! > "$HOME_DIR/server.pid"
  for _ in $(seq 60); do
    curl -fs -m 2 -o /dev/null http://localhost:8069/web/login && return 0
    sleep 1
  done
  echo "Odoo did not start; see $HOME_DIR/server.log" >&2
  return 1
}

stop() {
  if [ -f "$HOME_DIR/server.pid" ]; then
    kill "$(cat "$HOME_DIR/server.pid")" 2>/dev/null || true
    rm -f "$HOME_DIR/server.pid"
    sleep 2
  fi
}

case "${1:-}" in
  install)
    mkdir -p "$HOME_DIR/src"
    curl -fsSL https://nightly.odoo.com/17.0/nightly/src/odoo_17.0.latest.tar.gz \
      | tar xz -C "$HOME_DIR/src" --strip-components=1
    # python-ldap needs system headers and is not used here; psycopg2 comes as a wheel.
    grep -v -i -E "^python-ldap|^psycopg2" "$HOME_DIR/src/requirements.txt" > "$HOME_DIR/req.txt"
    echo "psycopg2-binary==2.9.9" >> "$HOME_DIR/req.txt"
    python3.11 -m venv "$HOME_DIR/venv"
    "$HOME_DIR/venv/bin/pip" install -q -r "$HOME_DIR/req.txt"
    "$HOME_DIR/venv/bin/pip" install -q --no-deps -e "$HOME_DIR/src"
    cat > "$HOME_DIR/odoo.conf" <<CONF
[options]
db_host = $PGHOST
db_user = $PGUSER
admin_passwd = local-only
addons_path = $HOME_DIR/src/odoo/addons
http_port = 8069
data_dir = $HOME_DIR/data
CONF
    ;;
  reset)
    stop
    dropdb --force --if-exists acme
    "$HOME_DIR/venv/bin/odoo" -c "$HOME_DIR/odoo.conf" -d acme \
      -i crm,sale_management,sale_crm,account --stop-after-init > "$HOME_DIR/init.log" 2>&1
    start
    ;;
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 install|reset|start|stop" >&2; exit 2 ;;
esac
