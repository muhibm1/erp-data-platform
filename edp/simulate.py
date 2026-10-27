"""Run a small company's year through a real Odoo, so the data has real shape.

Everything goes through Odoo's own API and business logic: deals are won or
lost in the CRM, won deals become confirmed sales orders, orders are invoiced
through Odoo's invoicing wizard, and payments are registered against invoices,
some in part. Business dates (order, invoice, due and payment dates) are spread
over January to October 2026.

Phase "year" builds the clean history. Phase "mess" then does what real
operations do after the data team has already loaded it once:

1. a sales rep creates a customer that already exists, under a new spelling;
2. accounting reopens a posted invoice, changes it, and posts it again;
3. some customers are billed directly in euros, outside the sales flow;
4. a deal is marked won in the CRM but no order is ever created;
5. a draft invoice is deleted outright.

Each one is caught by a named dbt test (see docs/checks.md). The simulation
is seeded, so the same command builds the same company.
"""

import argparse
import datetime as dt
import json
import os
import random

from .odoo import Odoo

YEAR_START = dt.date(2026, 1, 5)
AS_OF = dt.date(2026, 10, 27)

CUSTOMERS = [
    "Northwind Logistics", "Bluebird Clinics", "Harbor Freight Partners", "Summit Roofing",
    "Cobalt Dental Group", "Larkspur Foods", "Ironwood Builders", "Meridian Insurance",
    "Pioneer Charter Schools", "Redline Auto Parts", "Silverline Hotels", "Tidewater Marine",
    "Orchard Valley Farms", "Keystone Legal", "Granite State Credit Union", "Beacon Pharmacy",
    "Copperleaf Apartments", "Driftwood Breweries", "Evergreen Home Health", "Foxglove Retail",
    "Glacier Water Co", "Highland Veterinary", "Juniper Architecture", "Kestrel Aviation Services",
    "Lighthouse Property Mgmt", "Maple Street Bakery", "Nimbus Print", "Oakhurst Dental",
    "Pinecrest Realty", "Quarry Lane Fitness",
]

PRODUCTS = [  # name, unit price
    ("ERP implementation (day)", 1800.0),
    ("Data migration package", 12000.0),
    ("Annual support plan", 9600.0),
    ("User training (session)", 950.0),
    ("Integration build (per system)", 7500.0),
]


def _d(day: dt.date) -> str:
    return day.isoformat()


class Company:
    """Wraps the Odoo calls the simulation needs, with readable names."""

    def __init__(self, odoo: Odoo, rng: random.Random):
        self.o, self.rng = odoo, rng
        self.stage_won = self._one("crm.stage", [("is_won", "=", True)])
        self.term_30 = self._one("account.payment.term", [("name", "ilike", "30 Days")])
        self.products = [self._product(n, p) for n, p in PRODUCTS]

    def _one(self, model, domain):
        ids = self.o.execute(model, "search", domain, limit=1)
        if not ids:
            raise RuntimeError(f"no {model} matching {domain}")
        return ids[0]

    def _product(self, name, price):
        found = self.o.execute("product.product", "search", [("name", "=", name)])
        if found:
            return found[0]
        return self.o.execute("product.product", "create", {
            "name": name, "list_price": price, "type": "service",
            "invoice_policy": "order", "sale_ok": True})

    def customer(self, name, email=None):
        email = email or name.lower().replace(" ", ".").replace("&", "and") + "@example.com"
        found = self.o.execute("res.partner", "search", [("name", "=", name)])
        if found:
            return found[0]
        return self.o.execute("res.partner", "create", {
            "name": name, "is_company": True, "email": email, "customer_rank": 1})

    def lines(self, target: float):
        """Order lines that add up to roughly the deal's expected revenue."""
        picks = self.rng.sample(range(len(PRODUCTS)), self.rng.randint(1, 3))
        share = target * self.rng.uniform(0.85, 1.2) / len(picks)
        lines = []
        for i in picks:
            qty = max(1, round(share / PRODUCTS[i][1]))
            # A negotiated price near list, so the order lands near the deal value.
            # US services, so no sales tax line.
            lines.append((0, 0, {"product_id": self.products[i], "product_uom_qty": qty,
                                 "price_unit": round(share / qty, -1), "tax_id": [(6, 0, [])]}))
        return lines

    def deal(self, partner, revenue, opened: dt.date):
        return self.o.execute("crm.lead", "create", {
            "name": f"Deal {opened:%b} {self.rng.randint(100, 999)}", "type": "opportunity",
            "partner_id": partner, "expected_revenue": revenue,
            "date_deadline": _d(opened + dt.timedelta(days=30))})

    def order(self, partner, deal, day: dt.date):
        target = self.o.execute("crm.lead", "read", [deal], ["expected_revenue"])[0]["expected_revenue"]
        so = self.o.execute("sale.order", "create", {
            "partner_id": partner, "opportunity_id": deal, "order_line": self.lines(target),
            "payment_term_id": self.term_30})
        self.o.execute("sale.order", "action_confirm", [so])
        self.o.execute("sale.order", "write", [so], {"date_order": f"{_d(day)} 15:00:00"})
        return so

    def close(self, deal, won: bool, day: dt.date):
        """Wins or loses a deal, dated on the business day it closed."""
        self.o.execute("crm.lead", "action_set_won" if won else "action_set_lost", [deal])
        self.o.execute("crm.lead", "write", [deal], {"date_closed": f"{_d(day)} 16:00:00"})

    def invoice_order(self, so, day: dt.date):
        ctx = {"active_model": "sale.order", "active_ids": [so], "active_id": so}
        wiz = self.o.execute("sale.advance.payment.inv", "create",
                             {"advance_payment_method": "delivered"}, context=ctx)
        self.o.execute("sale.advance.payment.inv", "create_invoices", [wiz], context=ctx)
        inv = self.o.execute("sale.order", "read", [so], ["invoice_ids"])[0]["invoice_ids"][-1]
        return self.post(inv, day)

    def post(self, inv, day: dt.date):
        self.o.execute("account.move", "write", [inv], {
            "invoice_date": _d(day), "invoice_payment_term_id": self.term_30})
        self.o.execute("account.move", "action_post", [inv])
        return inv

    def pay(self, inv, amount, day: dt.date):
        ctx = {"active_model": "account.move", "active_ids": [inv]}
        wiz = self.o.execute("account.payment.register", "create",
                             {"amount": round(amount, 2), "payment_date": _d(day)}, context=ctx)
        self.o.execute("account.payment.register", "action_create_payments", [wiz], context=ctx)

    def direct_invoice(self, partner, currency, amount, day: dt.date, post=True):
        inv = self.o.execute("account.move", "create", {
            "move_type": "out_invoice", "partner_id": partner, "currency_id": currency,
            "invoice_line_ids": [(0, 0, {"name": "Consulting, billed directly", "quantity": 1,
                                         "price_unit": amount, "tax_ids": [(6, 0, [])]})]})
        return self.post(inv, day) if post else inv


def year(c: Company) -> dict:
    """Ten months of deals, orders, invoices and payments."""
    rng = c.rng
    customers = [c.customer(n) for n in CUSTOMERS]
    log = {"deals": 0, "won": 0, "lost": 0, "open": 0, "invoices": 0, "payments": 0}
    for week in range(42):
        opened = YEAR_START + dt.timedelta(weeks=week)
        for _ in range(rng.randint(2, 5)):
            partner = rng.choice(customers)
            deal = c.deal(partner, rng.choice([5000, 9600, 12000, 18000, 25000, 40000]), opened)
            log["deals"] += 1
            fate = rng.random()
            close = opened + dt.timedelta(days=rng.randint(7, 45))
            if close >= AS_OF or fate > 0.85:
                log["open"] += 1
                continue
            if fate > 0.55:
                c.close(deal, False, close)
                log["lost"] += 1
                continue
            c.close(deal, True, close)
            log["won"] += 1
            so = c.order(partner, deal, close)
            billed = close + dt.timedelta(days=rng.randint(0, 10))
            if billed >= AS_OF:
                continue
            inv = c.invoice_order(so, billed)
            log["invoices"] += 1
            total = c.o.execute("account.move", "read", [inv], ["amount_total"])[0]["amount_total"]
            habit = rng.random()  # most pay on time, some late, some in part, some never
            if habit < 0.55:
                paid_on = billed + dt.timedelta(days=rng.randint(10, 35))
                share = 1.0
            elif habit < 0.75:
                paid_on = billed + dt.timedelta(days=rng.randint(45, 95))
                share = 1.0
            elif habit < 0.9:
                paid_on = billed + dt.timedelta(days=rng.randint(20, 60))
                share = rng.choice([0.25, 0.5])
            else:
                continue
            if paid_on < AS_OF:
                c.pay(inv, total * share, paid_on)
                log["payments"] += 1
    return log


def mess(c: Company) -> dict:
    """The five things operations do to data after it has been loaded once."""
    o, rng = c.o, c.rng
    done = {}
    # 1. The same customer, created again by a different rep.
    dup = c.customer("Northwind Logistics Inc.", email="AP@Northwind-Logistics.example.com")
    o.execute("res.partner", "write",
              [c.customer("Northwind Logistics")], {"email": "ap@northwind-logistics.example.com"})
    deal = c.deal(dup, 18000, dt.date(2026, 9, 14))
    c.close(deal, True, dt.date(2026, 9, 18))
    inv = c.invoice_order(c.order(dup, deal, dt.date(2026, 9, 21)), dt.date(2026, 9, 22))
    done["duplicate_customer"] = {"partner": dup, "invoice": inv}
    # 2. A posted invoice reopened, changed and posted again.
    posted = o.execute("account.move", "search", [
        ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
        ("payment_state", "=", "not_paid"), ("invoice_origin", "!=", False)], limit=1, order="id asc")[0]
    o.execute("account.move", "button_draft", [posted])
    line = o.execute("account.move", "read", [posted], ["invoice_line_ids"])[0]["invoice_line_ids"][0]
    qty = o.execute("account.move.line", "read", [line], ["quantity"])[0]["quantity"]
    o.execute("account.move", "write", [posted], {"invoice_line_ids": [(1, line, {"quantity": qty + 2})]})
    o.execute("account.move", "action_post", [posted])
    done["edited_after_post"] = posted
    # 3. Customers billed directly in euros.
    eur = o.execute("res.currency", "search", [("name", "=", "EUR"), ("active", "in", [True, False])])[0]
    o.execute("res.currency", "write", [eur], {"active": True})
    euro = []
    for name, amount, day in [("Kestrel Aviation Services", 14000, dt.date(2026, 8, 3)),
                              ("Juniper Architecture", 6200, dt.date(2026, 9, 9)),
                              ("Silverline Hotels", 9800, dt.date(2026, 10, 1))]:
        euro.append(c.direct_invoice(c.customer(name), eur, amount, day))
    c.pay(euro[0], 14000, dt.date(2026, 9, 2))
    done["euro_invoices"] = euro
    # 4. Won in the CRM, never turned into an order.
    ghost = c.deal(c.customer("Beacon Pharmacy"), 25000, dt.date(2026, 9, 28))
    c.close(ghost, True, dt.date(2026, 10, 6))
    done["won_without_order"] = ghost
    # 5. A draft invoice that will be deleted after the next load.
    draft = c.direct_invoice(c.customer("Nimbus Print"), o.execute(
        "res.currency", "search", [("name", "=", "USD")])[0], 3100, dt.date(2026, 10, 20), post=False)
    done["draft_to_delete"] = draft
    return done


def delete_draft(odoo: Odoo, draft: int) -> None:
    odoo.execute("account.move", "unlink", [draft])


def main(argv=None):
    p = argparse.ArgumentParser(prog="edp-simulate")
    p.add_argument("phase", choices=["year", "mess", "delete-draft"])
    p.add_argument("--draft", type=int)
    p.add_argument("--seed", type=int, default=2026)
    args = p.parse_args(argv)
    odoo = Odoo(os.environ.get("ODOO_URL", "http://localhost:8069"), os.environ.get("ODOO_DB", "acme"),
                os.environ.get("ODOO_USER", "admin"), os.environ.get("ODOO_PASSWORD", "admin"))
    if args.phase == "delete-draft":
        delete_draft(odoo, args.draft)
        return
    c = Company(odoo, random.Random(args.seed if args.phase == "year" else args.seed + 1))
    print(json.dumps(year(c) if args.phase == "year" else mess(c), indent=2))


if __name__ == "__main__":
    main()
