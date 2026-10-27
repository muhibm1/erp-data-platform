#!/usr/bin/env bash
# Runs Odoo 17 Community from source against a local Postgres, with demo data
# and the CRM, Sales and Invoicing apps. Usage:
#   scripts/odoo-local.sh install   # download Odoo 17 and its Python packages into ./.odoo
#   scripts/odoo-local.sh reset     # drop and recreate the "acme" database, then start the server
# Needs Python 3.11, uv, and a Postgres 16 server you can reach on a socket in /tmp
# as a role named odoo that can create databases.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$ROOT/.odoo"
case "${1:-}" in
  install)
    mkdir -p "$HOME_DIR/src"
    curl -fsSL https://nightly.odoo.com/17.0/nightly/src/odoo_17.0.latest.tar.gz \
      | tar xz -C "$HOME_DIR/src" --strip-components=1
    # python-ldap needs system headers and is not used here; psycopg2 comes as a wheel.
    grep -v -i -E "^python-ldap|^psycopg2" "$HOME_DIR/src/requirements.txt" > "$HOME_DIR/req.txt"
    echo "psycopg2-binary==2.9.9" >> "$HOME_DIR/req.txt"
    uv venv -q -p 3.11 "$HOME_DIR/venv"
    uv pip install -q -p "$HOME_DIR/venv/bin/python" -r "$HOME_DIR/req.txt"
    uv pip install -q -p "$HOME_DIR/venv/bin/python" -e "$HOME_DIR/src" --no-deps
    cat > "$HOME_DIR/odoo.conf" <<CONF
[options]
db_host = /tmp
db_user = odoo
admin_passwd = local-only
addons_path = $HOME_DIR/src/odoo/addons
http_port = 8069
data_dir = $HOME_DIR/data
CONF
    ;;
  reset)
    if [ -f "$HOME_DIR/server.pid" ]; then kill "$(cat "$HOME_DIR/server.pid")" 2>/dev/null || true; sleep 2; fi
    dropdb -h /tmp -U odoo --force --if-exists acme
    "$HOME_DIR/venv/bin/odoo" -c "$HOME_DIR/odoo.conf" -d acme \
      -i crm,sale_management,sale_crm,account --stop-after-init > "$HOME_DIR/init.log" 2>&1
    nohup "$HOME_DIR/venv/bin/odoo" -c "$HOME_DIR/odoo.conf" -d acme --db-filter=acme \
      > "$HOME_DIR/server.log" 2>&1 &
    echo $! > "$HOME_DIR/server.pid"
    sleep 8
    ;;
  *) echo "usage: $0 install|reset" >&2; exit 2 ;;
esac
