"""Builds the whole demo from an empty Odoo database, in the order it happens.

1. Simulate January to October in Odoo, then load the warehouse.
2. Operations make five messy changes, then the warehouse loads again.
3. A draft invoice is deleted in Odoo, and the warehouse loads a third time.
4. dbt builds the models and runs every check.
"""

import json
import os
import random
import subprocess
import sys

import psycopg

from . import simulate
from .load import extract
from .odoo import Odoo


def main():
    odoo = Odoo(os.environ.get("ODOO_URL", "http://localhost:8069"), os.environ.get("ODOO_DB", "acme"),
                os.environ.get("ODOO_USER", "admin"), os.environ.get("ODOO_PASSWORD", "admin"))
    dsn = os.environ.get("D2C_DSN", "host=/tmp dbname=warehouse user=odoo")
    with psycopg.connect(dsn) as conn:
        print("year:", json.dumps(simulate.year(simulate.Company(odoo, random.Random(2026)))))
        print("load 1:", json.dumps(extract(odoo, conn)))
        mess = simulate.mess(simulate.Company(odoo, random.Random(2027)))
        print("mess:", json.dumps(mess))
        print("load 2:", json.dumps(extract(odoo, conn)))
        simulate.delete_draft(odoo, mess["draft_to_delete"])
        print("load 3:", json.dumps(extract(odoo, conn)))
    dbt = os.path.join(os.path.dirname(sys.executable), "dbt")
    sys.exit(subprocess.call([dbt, "build", "--project-dir", "dbt", "--profiles-dir", "dbt"]))


if __name__ == "__main__":
    main()
