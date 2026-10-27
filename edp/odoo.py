"""A small Odoo JSON-RPC client, standard library only."""

import itertools
import json
import socket
import time
import urllib.error
import urllib.request


class OdooError(RuntimeError):
    """Odoo answered with an error payload."""


class OdooUnavailable(OdooError):
    """Odoo could not be reached after every retry."""


class Odoo:
    """Talks to one Odoo database as one user over /jsonrpc."""

    def __init__(self, url: str, db: str, user: str, password: str, timeout: float = 60,
                 retries: int = 4, backoff: float = 2.0):
        self.url = url.rstrip("/") + "/jsonrpc"
        self.db, self.password, self.timeout = db, password, timeout
        self.retries, self.backoff = retries, backoff
        self._ids = itertools.count(1)
        self.uid = self._call("common", "login", db, user, password)
        if not self.uid:
            raise OdooError(f"login refused for {user!r} on {db!r}")

    def _call(self, service: str, method: str, *args):
        body = json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": next(self._ids),
            "params": {"service": service, "method": method, "args": args},
        }).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        # Network errors and 5xx are retried with backoff (2 s, 4 s, 8 s, 16 s).
        # An error Odoo answers with (bad field, access denied) is not: retrying cannot fix it.
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    reply = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or attempt == self.retries:
                    raise OdooUnavailable(f"{self.url}: HTTP {exc.code}") from exc
            except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
                if attempt == self.retries:
                    raise OdooUnavailable(f"{self.url}: {exc}") from exc
            time.sleep(self.backoff * 2 ** attempt)
        if "error" in reply:
            err = reply["error"]
            raise OdooError(err.get("data", {}).get("message") or err.get("message"))
        return reply.get("result")

    def execute(self, model: str, method: str, *args, **kwargs):
        """Calls a model method, as the logged-in user."""
        return self._call("object", "execute_kw", self.db, self.uid, self.password,
                          model, method, list(args), kwargs)

    def fields(self, model: str) -> dict[str, bool]:
        """Every field a model has on this server, mapped to whether it is stored."""
        meta = self.execute(model, "fields_get", attributes=["store"])
        return {name: bool(m.get("store")) for name, m in meta.items()}

    def search_read(self, model: str, domain: list, fields: list[str], batch: int = 500):
        """Yields every matching record, oldest change first, in pages."""
        offset = 0
        while True:
            page = self.execute(model, "search_read", domain, fields=fields,
                                order="write_date asc, id asc", limit=batch, offset=offset)
            yield from page
            if len(page) < batch:
                return
            offset += batch
