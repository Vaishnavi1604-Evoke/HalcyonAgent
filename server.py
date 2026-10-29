"""Run the live order agent, the order simulator and the Halcyon UI.

    python server.py            then open http://localhost:8000/login.html

Background services:
  * order simulator   creates today's orders and releases them in real time (SIMULATOR=on)
  * email watcher     reads the order mailbox every POLL_SECONDS
  * EDI watcher       reads X12 files dropped into inbox/edi_drop/
  * lifecycle         moves allocated orders to picking, shipped and delivered

API:
  GET  /api/summary                 counts by channel today, watcher status, latest orders
  GET  /api/orders                  live orders; filters: date, channel, warehouse, carrier, status, stage, q, limit, offset
  GET  /api/orders/<delivery_id>    one order with every column, including the raw message
  GET  /api/allocation              today's allocation by warehouse, carrier, wave and status, plus stock
  POST /api/orders                  submit an API order (JSON)
  GET  /api/mailbox                 is a real mailbox linked and fetching; matching emails and the orders made from them
  GET  /api/deliveries              shipments on the road with their ETA and line-up position; deliveries made
  GET  /api/policy                  today's results of the five allocation rules vs the 95% target
  POST /api/orders/<id>/address     confirm a held address; the order is then allocated
  GET  /api/tracking                tracking events received today, latest events, orders waiting on batch/portal carriers
  GET  /api/orders/<id>/events      an order's tracking events
  POST /api/tracking-events         carrier / WMS webhook: {delivery_id, event_type: PICKED|COLLECTED|DELIVERED, event_time, ...}
  POST /api/email-order             read a pasted email as if it arrived in the mailbox
  POST /api/check-mail              check the mailbox now
"""
import json, os
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from halcyon.live import LiveService
from halcyon.store import ALL_COLS
from halcyon import backtest
BACKTEST = {}

ROOT = Path(__file__).resolve().parent
svc = LiveService()
LIST_COLS = ["delivery_id", "order_id", "client_id", "client_name", "order_channel", "order_received_ts", "commitment_type",
             "promised_date", "address_status", "destination_region", "nearest_warehouse_id", "fulfilment_warehouse_id",
             "nearest_warehouse_had_stock", "carrier_id", "carrier_rate_usd", "pick_wave", "pick_list_date", "agent_status",
             "stage", "priority", "sku", "qty", "shipped_ts", "delivered_date", "on_time", "extraction_confidence", "source_file", "eta", "route_stop", "transit_miles"]
FILTERS = {"channel": "order_channel", "warehouse": "fulfilment_warehouse_id", "carrier": "carrier_id",
           "status": "agent_status", "stage": "stage", "region": "destination_region"}


def list_orders(qs):
    day = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
    where, args = ["order_received_ts LIKE ?"], [day + "%"]
    for k, col in FILTERS.items():
        v = qs.get(k, [""])[0]
        if v:
            vals = [x for x in v.split(",") if x]
            where.append(f"{col} IN ({','.join('?' * len(vals))})"); args += vals
    if qs.get("exceptions", [""])[0] == "1": where.append("agent_status != 'ASSIGNED'")
    since = qs.get("since", [""])[0]
    if since: where.append("CAST(SUBSTR(delivery_id,2) AS INTEGER) > ?"); args.append(int(since.lstrip("D")))
    q = qs.get("q", [""])[0].strip()
    if q:
        where.append("(delivery_id LIKE ? OR order_id LIKE ? OR client_id LIKE ? OR client_name LIKE ?)"); args += [f"%{q}%"] * 4
    w = " AND ".join(where)
    limit = min(int(qs.get("limit", ["50"])[0]), 500); offset = int(qs.get("offset", ["0"])[0])
    total = svc.store.rows(f"SELECT COUNT(*) n FROM orders WHERE {w}", args)[0]["n"]
    rows = svc.store.rows(f"SELECT {','.join(LIST_COLS)} FROM orders WHERE {w} ORDER BY order_received_ts DESC, delivery_id DESC LIMIT ? OFFSET ?", args + [limit, offset])
    return dict(date=day, total=total, rows=rows)


def allocation(qs):
    day = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]; a = [day + "%"]
    g = lambda sql: svc.store.rows(sql, a)
    stock_now = svc.stock(); opening = svc.store.meta().get("opening_stock", {})
    return dict(
        date=day,
        by_status={r["k"]: r["n"] for r in g("SELECT agent_status k, COUNT(*) n FROM orders WHERE order_received_ts LIKE ? GROUP BY k")},
        by_stage={r["k"]: r["n"] for r in g("SELECT stage k, COUNT(*) n FROM orders WHERE order_received_ts LIKE ? GROUP BY k")},
        by_carrier={r["k"]: r["n"] for r in g("SELECT carrier_id k, COUNT(*) n FROM orders WHERE order_received_ts LIKE ? AND carrier_id IS NOT NULL GROUP BY k")},
        warehouses=g("""SELECT fulfilment_warehouse_id warehouse, COUNT(*) orders,
              SUM(pick_wave LIKE 'W1%' AND pick_list_date = substr(order_received_ts,1,10)) wave1,
              SUM(pick_wave LIKE 'W2%') wave2, SUM(pick_list_date > substr(order_received_ts,1,10)) tomorrow,
              SUM(nearest_warehouse_had_stock = 'N') fallback, SUM(qty) units
              FROM orders WHERE order_received_ts LIKE ? AND fulfilment_warehouse_id IS NOT NULL GROUP BY warehouse ORDER BY warehouse"""),
        matrix=g("SELECT fulfilment_warehouse_id warehouse, carrier_id carrier, COUNT(*) n FROM orders WHERE order_received_ts LIKE ? AND carrier_id IS NOT NULL GROUP BY 1,2"),
        restock=g("SELECT restock_alert alert, COUNT(*) n FROM orders WHERE order_received_ts LIKE ? AND restock_alert IS NOT NULL GROUP BY 1 ORDER BY n DESC"),
        stock={w: dict(now=sum(stock_now.get(w, {}).values()), opening=sum(opening.get(w, {}).values()),
                       low=[s for s, q in stock_now.get(w, {}).items() if q < 45]) for w in sorted(opening)})


def policy(qs):
    """Today's results of the five allocation rules, against the 95% target."""
    day = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]; a = [day + "%"]
    if not BACKTEST: BACKTEST.update(backtest.run())
    one = lambda sql: svc.store.rows(sql, a)[0]
    tot = one("SELECT COUNT(*) n, SUM(fulfilment_warehouse_id IS NOT NULL) alloc, AVG(CAST(expected_on_time AS REAL)) exp FROM orders WHERE order_received_ts LIKE ?")
    dl = one("SELECT COUNT(*) n, SUM(on_time='Y') ok FROM orders WHERE order_received_ts LIKE ? AND stage='Delivered'")
    held = one("SELECT SUM(agent_status='HELD_ADDRESS') addr, SUM(agent_status='HELD_STOCK') stock FROM orders WHERE order_received_ts LIKE ?")
    def cause(keys):
        cond = " OR ".join(f"(',' || causes || ',') LIKE '%,{k},%'" for k in keys)
        r = one(f"SELECT COUNT(*) n, AVG(CAST(expected_on_time AS REAL)) exp FROM orders WHERE order_received_ts LIKE ? AND ({cond})")
        return dict(n=r["n"], expected=r["exp"])
    inc = one("SELECT COUNT(*) n FROM orders WHERE order_received_ts LIKE ? AND address_status='incomplete'")["n"]
    rules = [
        dict(key="address", rule="Complete the address from the client’s known sites; if ambiguous, hold and confirm before the pick list",
             caught=dict(repaired=cause(["address_repaired"]), confirmed=cause(["address_confirmed"]), waiting=held["addr"] or 0, incomplete=inc)),
        dict(key="cutoff", rule="Wave 2 at 11:00 and a same-day express wave at 13:00; later same-day orders get an honest next-day date",
             caught=dict(wave2=cause(["cutoff_wave2"]), express=cause(["cutoff_express"]), repromised=cause(["cutoff_repromised"]))),
        dict(key="stock", rule="Stock placed each morning from the forecast, by where orders go; in-region sites first; restock alert otherwise",
             caught=dict(fallback=cause(["stock_fallback"]), held=held["stock"] or 0)),
        dict(key="carrier_d", rule="Carrier must be at least 90% on time from the origin warehouse; D never qualifies",
             caught=dict(avoided=cause(["carrier_d_avoided"]), used=one("SELECT COUNT(*) n FROM orders WHERE order_received_ts LIKE ? AND carrier_id='D'")["n"])),
        dict(key="weak_lane", rule="Reliability judged per origin, so F from W3/W4 and E from W5 are blocked",
             caught=dict(avoided=cause(["weak_lane_avoided"]), used=one("SELECT COUNT(*) n FROM orders WHERE order_received_ts LIKE ? AND ((carrier_id='F' AND fulfilment_warehouse_id IN ('W3','W4')) OR (carrier_id='E' AND fulfilment_warehouse_id='W5'))")["n"]))]
    return dict(date=day, orders=tot["n"], allocated=tot["alloc"] or 0, projected_today=tot["exp"], delivered=dl["n"],
                delivered_on_time=(dl["ok"] / dl["n"]) if dl["n"] else None, rules=rules, backtest=BACKTEST)


CONNECT = {}   # the sign-in in progress, if any


def set_env(**kv):
    """Persist settings to .env so they survive a restart."""
    path = ROOT / ".env"; lines = path.read_text().splitlines() if path.exists() else []
    for k, v in kv.items():
        os.environ[k] = v
        for i, l in enumerate(lines):
            if l.split("=", 1)[0].strip() == k: lines[i] = f"{k}={v}"; break
        else: lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")


def start_connect(j):
    from halcyon import outlook
    import threading
    cid = (j.get("client_id") or os.environ.get("OUTLOOK_CLIENT_ID", "")).strip()
    tenant = (j.get("tenant") or os.environ.get("OUTLOOK_TENANT", "consumers")).strip()
    subject = (j.get("subject") or os.environ.get("ORDER_SUBJECT", "New order arrived")).strip()
    if not cid: return {"state": "error", "error": "Enter the Application (client) ID from your Azure app registration."}
    import re
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", cid):
        return {"state": "error", "error": f"“{cid}” isn’t an Application (client) ID. It is not your username or email address: it is a 36-character "
                "code like 1a2b3c4d-1234-5678-9abc-1234567890ab, shown on the Overview page of the app you registered in the Azure portal."}
    try: dc = outlook.start_device_flow(cid, tenant)
    except Exception as e: return {"state": "error", "error": f"Microsoft rejected the request: {e}"}
    set_env(OUTLOOK_CLIENT_ID=cid, OUTLOOK_TENANT=tenant, ORDER_SUBJECT=subject)
    CONNECT.clear(); CONNECT.update(state="waiting", user_code=dc["user_code"], url=dc.get("verification_uri", "https://microsoft.com/devicelogin"),
                                    expires=dc["started"] + int(dc.get("expires_in", 900)), error="")
    def wait():
        try:
            outlook.poll_device_flow(dc)
            set_env(MAIL_MODE="outlook"); svc.use_outlook(True); CONNECT.update(state="connected")
        except Exception as e:
            CONNECT.update(state="error", error=str(e))
    threading.Thread(target=wait, daemon=True).start()
    return dict(CONNECT)


def deliveries(qs):
    """Live delivery view: what is on the road, when each one is due, what was delivered."""
    day = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]; a = (day + "%",)
    now = datetime.now().isoformat(timespec="seconds")
    road = svc.store.rows("""SELECT delivery_id, client_name, fulfilment_warehouse_id wh, ship_to, destination_region region, carrier_id,
        transit_miles, route_id, route_stop, eta, eta_wall, promised_date, commitment_type FROM orders
        WHERE order_received_ts LIKE ? AND stage='Shipped' AND eta IS NOT NULL ORDER BY eta_wall LIMIT 400""", a)
    from halcyon.transit import city_of
    for r in road:
        r["city"] = city_of(r["ship_to"]) or r["region"]; r["late_vs_eta"] = r["eta_wall"] < now
        r["due_in_s"] = (datetime.fromisoformat(r["eta_wall"]) - datetime.now()).total_seconds()
        r["eta_after_promise"] = r["eta"][:10] > (r["promised_date"] or "9999")
    dl = svc.store.rows("""SELECT COUNT(*) n, SUM(on_time='Y') ok, AVG(CAST(transit_miles AS REAL)) miles FROM orders
        WHERE order_received_ts LIKE ? AND stage='Delivered'""", a)[0]
    recent = svc.store.rows("""SELECT o.delivery_id, o.client_name, o.fulfilment_warehouse_id wh, o.ship_to, o.eta, e.event_time delivered_at,
        o.on_time, o.days_late, e.received_at FROM tracking_events e JOIN orders o USING(delivery_id)
        WHERE e.event_type='DELIVERED' AND o.order_received_ts LIKE ? ORDER BY e.event_id DESC LIMIT 8""", a)
    for r in recent: r["city"] = city_of(r["ship_to"])
    hour_ago = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    last_hour = svc.store.rows("SELECT COUNT(*) n FROM tracking_events WHERE event_type='DELIVERED' AND received_at >= ?", (hour_ago,))[0]["n"]
    return dict(on_road=len(road), running_late=sum(r["late_vs_eta"] for r in road), arriving=road[:10],
                delivered=dl["n"], delivered_on_time=(dl["ok"] / dl["n"]) if dl["n"] else None, delivered_last_hour=last_hour,
                recent=recent, mode=os.environ.get("SIM_MODE", "realtime"))


def mailbox_status():
    """Is a real mailbox linked, and is it fetching? Used by the Email connection screen."""
    m = svc.store.meta(); mode = m.get("mail_mode", "folder"); day = datetime.now().strftime("%Y-%m-%d")
    real = {"outlook": "outlook", "imap": "imap", "graph": "graph"}.get(mode)
    box = m.get(f"box_{real}", {}) if real else {}
    from halcyon.outlook import TOKEN_FILE
    state = ("demo" if not real else "not_signed_in" if mode == "outlook" and not TOKEN_FILE.exists()
             else "error" if box and not box.get("ok") else "connected" if box.get("ok") else "starting")
    rows = svc.store.rows("""SELECT e.checked_at, e.sender, e.subject, e.is_order, o.delivery_id, o.agent_status, o.fulfilment_warehouse_id
              FROM emails_seen e LEFT JOIN orders o ON o.uid = 'email:' || e.message_id
              WHERE e.mailbox = ? ORDER BY e.checked_at DESC LIMIT 25""", (real or "-",))
    counts = svc.store.summary()["email_sources"]
    return dict(connect=dict(CONNECT), client_id=os.environ.get("OUTLOOK_CLIENT_ID", ""), tenant=os.environ.get("OUTLOOK_TENANT", "consumers"),
                default_subject=os.environ.get("ORDER_SUBJECT", "New order arrived"),
                mode=mode, state=state, account=box.get("account") or m.get("mailbox") or "", subject=m.get("order_subject", ""),
                poll_seconds=m.get("poll_seconds"), last_check=box.get("checked") or m.get("last_check"), error=box.get("error", ""),
                scanned_today=box.get("scanned_today"), matched_today=box.get("matched_today"), recent_inbox=box.get("recent_inbox", []),
                orders=rows, counts=counts, simulated_email=m.get("simulated_email", False))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(ROOT / "ui"), **k)

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            if u.path == "/api/summary":
                s = svc.store.summary(); s["watcher"] = svc.store.meta(); s["watcher"].pop("opening_stock", None); return self._json(s)
            if u.path == "/api/orders": return self._json(list_orders(qs))
            if u.path.startswith("/api/orders/") and u.path.endswith("/events"):
                return self._json(svc.store.rows("SELECT event_type, source, carrier, event_time, happened_at, received_at FROM tracking_events WHERE delivery_id=? ORDER BY event_id", (u.path.split("/")[3],)))
            if u.path == "/api/tracking":
                day = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
                return self._json(dict(
                    by_source={r["source"]: r["n"] for r in svc.store.rows("SELECT source, COUNT(*) n FROM tracking_events WHERE received_at LIKE ? GROUP BY source", (day + "%",))},
                    recent=svc.store.rows("SELECT e.delivery_id, e.event_type, e.source, e.carrier, e.event_time, e.received_at, o.client_name FROM tracking_events e JOIN orders o USING(delivery_id) ORDER BY e.event_id DESC LIMIT 10"),
                    waiting=svc.store.rows("SELECT carrier_id carrier, COUNT(*) n FROM orders WHERE stage IN ('Picking','Shipped') AND carrier_id IN ('D','E','F') GROUP BY carrier_id")))
            if u.path.startswith("/api/orders/"):
                rows = svc.store.rows(f"SELECT {','.join(ALL_COLS)} FROM orders WHERE delivery_id = ?", (u.path.rsplit("/", 1)[1],))
                return self._json(rows[0]) if rows else self._json({"error": "not found"}, 404)
            if u.path == "/api/allocation": return self._json(allocation(qs))
            if u.path == "/api/policy": return self._json(policy(qs))
            if u.path == "/api/deliveries": return self._json(deliveries(qs))
            if u.path == "/api/mailbox": return self._json(mailbox_status())
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        if u.path in ("/", ""): self.path = "/login.html"
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n).decode() if n else ""
        if self.path == "/api/orders":
            try:
                r = svc.api_order(body)
                return self._json({"accepted": r is not None, **({k: r.get(k) for k in ["delivery_id", "agent_status", "fulfilment_warehouse_id", "carrier_id"]} if r else {})})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if self.path.startswith("/api/orders/") and self.path.endswith("/address"):
            j = json.loads(body or "{}")
            r = svc.resolve_address(self.path.split("/")[3], j.get("street", ""), j.get("postal_code", ""), by="customer success")
            return self._json({"allocated": bool(r and r.get("warehouse")), "status": r and r.get("status")})
        if self.path == "/api/tracking-events":   # webhook for API carriers (A, B, C) and the WMS
            evs = json.loads(body or "[]"); evs = evs if isinstance(evs, list) else [evs]
            ok = sum(bool(svc.receive_event(e, e.get("source") or "Carrier API")) for e in evs)
            return self._json({"applied": ok, "received": len(evs)})
        if self.path == "/api/email-order":
            r = svc.email_from_text(body)
            return self._json({"accepted": bool(r), "delivery_id": r and r["delivery_id"], "reason": None if r else "No order found in this email"})
        if self.path == "/api/outlook/connect":
            return self._json(start_connect(json.loads(body or "{}")))
        if self.path == "/api/outlook/disconnect":
            from halcyon import outlook
            outlook.sign_out(); set_env(MAIL_MODE="folder"); svc.use_outlook(False); CONNECT.clear()
            return self._json({"ok": True})
        if self.path == "/api/simulator/emails":
            on = bool(json.loads(body or "{}").get("on")); svc.set_demo_emails(on)
            set_env(SIM_CHANNELS="EDI,Email" if on else "EDI"); return self._json({"on": on})
        if self.path == "/api/check-mail":
            svc.wake.set(); return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith(("POST", "GET /api/check")): super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    svc.start()
    print(f"Order simulator: {'on, ' + os.environ.get('SIM_MODE', 'realtime') if svc.sim_on else 'off'}")
    print(f"Watching {svc.mailbox_addr} ({os.environ.get('MAIL_MODE', 'folder')} mode, every {svc.poll}s)")
    print(f"Open http://localhost:{port}/login.html")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
