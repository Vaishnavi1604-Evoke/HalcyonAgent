"""Live ingestion: watch the order mailbox, the EDI drop folder and the API endpoint,
and pass every order to the agent.

Mailbox backends (set MAIL_MODE in .env):
  graph   Microsoft 365 via Microsoft Graph (recommended for Outlook/Exchange Online;
          basic-auth IMAP is usually disabled there). Needs an Azure app registration
          with the Mail.Read application permission, scoped to the order mailbox.
  imap    Any IMAP server (Gmail, Zoho, on-prem Exchange) with an app password.
  folder  Demo/offline: reads .eml files dropped into inbox/mail_drop/.
"""
from __future__ import annotations
import email, imaplib, json, os, random, zlib, threading, time, traceback, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from email import policy
from pathlib import Path
from .common import ROOT, SKUS, PROMISE_DAYS, client_domain, load_reference
from .parsers import parse_email, parse_edi, parse_api
from .agent import OrderAgent
from .simulate import placed_inventory
from .store import Store


def load_env(path=ROOT / ".env"):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"'))


# ---------------- mailbox backends: each returns [(message_id, raw_bytes)] for today ----------------
class FolderMailbox:
    name = "simulated"      # the order simulator's emails (or .eml files dropped in by hand)
    def __init__(self): self.dir = ROOT / "inbox" / "mail_drop"; self.dir.mkdir(parents=True, exist_ok=True)
    def fetch(self):
        return [(f"file:{f.name}", f.read_bytes()) for f in sorted(self.dir.glob("*.eml"))]


class ImapMailbox:
    name = "imap"
    def __init__(self):
        self.host = os.environ["IMAP_HOST"]; self.user = os.environ["IMAP_USER"]; self.pw = os.environ["IMAP_PASSWORD"]
        self.folder = os.environ.get("IMAP_FOLDER", "INBOX")
    def fetch(self):
        out = []
        with imaplib.IMAP4_SSL(self.host) as m:
            m.login(self.user, self.pw); m.select(self.folder, readonly=True)   # read-only: never marks mail as read
            since = datetime.now().strftime("%d-%b-%Y")
            subj = os.environ.get("ORDER_SUBJECT", "")
            _, ids = m.search(None, "SINCE", since, *(["SUBJECT", f'"{subj}"'] if subj else []))
            for i in ids[0].split():
                _, data = m.fetch(i, "(BODY.PEEK[])")
                raw = data[0][1]
                mid = email.message_from_bytes(raw, policy=policy.default).get("Message-ID") or f"imap:{i.decode()}"
                out.append((mid, raw))
        return out


class GraphMailbox:
    name = "graph"
    def __init__(self):
        self.tenant = os.environ["GRAPH_TENANT_ID"]; self.cid = os.environ["GRAPH_CLIENT_ID"]
        self.secret = os.environ["GRAPH_CLIENT_SECRET"]; self.box = os.environ["ORDER_MAILBOX"]
        self.token, self.exp = None, 0
    def _token(self):
        if self.token and time.time() < self.exp - 60: return self.token
        body = urllib.parse.urlencode(dict(client_id=self.cid, client_secret=self.secret, grant_type="client_credentials",
                                           scope="https://graph.microsoft.com/.default")).encode()
        r = json.load(urllib.request.urlopen(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token", body, timeout=20))
        self.token, self.exp = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self.token
    def _get(self, url, raw=False):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token()})
        with urllib.request.urlopen(req, timeout=30) as r: return r.read() if raw else json.load(r)
    def fetch(self):
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        q = urllib.parse.urlencode({"$filter": f"receivedDateTime ge {start}", "$select": "id,internetMessageId", "$top": "100"})
        url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(self.box)}/mailFolders/inbox/messages?{q}"
        out = []
        while url:
            page = self._get(url)
            for m in page.get("value", []):
                raw = self._get(f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(self.box)}/messages/{m['id']}/$value", raw=True)
                out.append((m.get("internetMessageId") or m["id"], raw))
            url = page.get("@odata.nextLink")
        return out


def make_mailbox():
    mode = os.environ.get("MAIL_MODE", "folder").lower()
    if mode == "outlook":
        from .outlook import OutlookMailbox
        return OutlookMailbox()
    return {"graph": GraphMailbox, "imap": ImapMailbox}.get(mode, FolderMailbox)()


# ---------------- the live service ----------------
ADDR_FIELDS = {"street", "city", "state", "postal_code", "postal_code (invalid)"}


class LiveService:
    def __init__(self):
        load_env()
        self.store = Store()
        clients, dist, _ = load_reference()
        self.dist = dist
        self.directory = {client_domain(n): c for c, n in zip(clients.client_id, clients.client_name)}
        # Real client sender addresses can be mapped in .env: CLIENT_SENDERS=buyer@acme.com:C001,orders@foo.com:C002
        for pair in filter(None, os.environ.get("CLIENT_SENDERS", "").split(",")):
            addr, cid = pair.split(":"); self.directory[addr.strip().lower()] = cid.strip()
        self.names = dict(zip(clients.client_id, clients.client_name))
        self.mailbox_addr = os.environ.get("ORDER_MAILBOX", "")   # a label only; the real account comes from the sign-in
        self.poll = int(os.environ.get("POLL_SECONDS", "15"))
        self.sim_on = os.environ.get("SIMULATOR", "on").lower() != "off"
        mode = os.environ.get("MAIL_MODE", "folder").lower()
        self.mailboxes = [make_mailbox()]
        if self.sim_on and mode != "folder": self.mailboxes.append(FolderMailbox())  # simulated emails alongside the real mailbox
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.day = None; self.agent = None
        self.store.set_meta(mailbox=self.mailbox_addr if mode != "folder" else "", mail_mode=mode, poll_seconds=self.poll, watcher="starting",
                            order_subject=os.environ.get("ORDER_SUBJECT", "New order arrived") if mode != "folder" else "",
                            simulated_email="Email" in os.environ.get("SIM_CHANNELS", "EDI,Email") and self.sim_on,
                            simulator="on" if self.sim_on else "off")

    # ---------- daily agent: fresh stock and carrier capacity each day ----------
    def new_day(self, day, inventory=None):
        with self.lock:
            if inventory is None:
                p = ROOT / "inbox" / day / "inventory.json"
                inventory = json.loads(p.read_text()) if p.exists() else placed_inventory(day)
            self.agent = OrderAgent(inventory); self.day = day
            self.store.set_meta(agent_day=day, opening_stock=inventory)

    # ---------- connect / disconnect Outlook from the Email connection screen ----------
    def use_outlook(self, on: bool):
        """Switch the running agent between the demo mailbox and the signed-in Outlook account (no restart)."""
        from .outlook import OutlookMailbox
        self.mailboxes = [b for b in self.mailboxes if b.name == "simulated"] or [FolderMailbox()]
        if on:
            os.environ["MAIL_MODE"] = "outlook"; self.mailboxes.insert(0, OutlookMailbox())
        else:
            os.environ["MAIL_MODE"] = "folder"
        mode = os.environ["MAIL_MODE"]
        self.store.set_meta(mail_mode=mode, order_subject=os.environ.get("ORDER_SUBJECT", "New order arrived") if on else "",
                            mailbox="" if not on else self.store.meta().get("mailbox", ""), box_outlook={})
        self.wake.set()

    def set_demo_emails(self, on: bool):
        sim = getattr(self, "sim", None)
        if sim: (sim.channels.add if on else sim.channels.discard)("Email")
        self.store.set_meta(simulated_email=bool(on and sim))

    def replenish(self, fraction):
        """Inbound stock delivery: top every SKU up by a share of the day's opening stock."""
        with self.lock:
            opening = self.store.meta().get("opening_stock", {})
            for w, skus in opening.items():
                for s, q in skus.items(): self.agent.stock[w][s] = self.agent.stock[w].get(s, 0) + int(q * fraction)

    def stock(self):
        with self.lock: return self.agent.stock if self.agent else {}

    @staticmethod
    def looks_like_order(o):
        return bool(o.order_ref) or (bool(o.sku) and o.qty > 0)

    # ---------- read -> allocate -> insert one row ----------
    def ingest(self, uid, channel, o, raw="", layout=None, source=None):
        if self.store.has_uid(uid): return None
        day = o.received[:10] or datetime.now().strftime("%Y-%m-%d")
        with self.lock:
            if self.day != day and day == datetime.now().strftime("%Y-%m-%d"): self.new_day(day)
            if self.agent is None: self.new_day(datetime.now().strftime("%Y-%m-%d"))
            addr_incomplete = any(m in ADDR_FIELDS for m in o.missing)   # state when ingested (data dictionary)
            r = self.agent.decide(o)
        region = o.region
        nearest = r.get("nearest_warehouse") or (self.dist[region].idxmin() if region in self.dist else "")
        wh = r.get("warehouse")
        from datetime import date as _d, timedelta
        promised = r.get("promised_date") or (str(_d.fromisoformat(day) + timedelta(days=PROMISE_DAYS.get(o.service, 4))) if o.service else "")
        h = zlib.crc32(uid.encode()) % 1000
        row = dict(
            uid=uid, order_id=o.order_ref, client_id=o.client_id, client_name=o.client_name or self.names.get(o.client_id, ""),
            order_channel=channel, email_format_id=layout if channel == "Email" else None,
            order_received_ts=o.received, commitment_type=o.service, promised_date=promised,
            address_status="incomplete" if addr_incomplete else "complete",
            pick_list_date=r.get("pick_date"), destination_region=region, nearest_warehouse_id=nearest,
            fulfilment_warehouse_id=wh, nearest_warehouse_had_stock=("Y" if wh == nearest else "N") if wh else None,
            distance_miles=r.get("miles"), carrier_id=r.get("carrier"),
            carrier_rate_usd=round(r["carrier_rate"] * (0.92 + 0.16 * h / 1000), 2) if r.get("carrier_rate") else None,
            sku=o.sku, qty=o.qty, ship_to=", ".join(x for x in [o.street, o.dock, o.city, o.state, o.postal_code] if x),
            agent_status=r.get("status"), agent_reason=r.get("reason") or r.get("log") or "",
            pick_wave=r.get("wave"), lane_on_time=r.get("lane_on_time"), priority="Y" if r.get("priority") else "N",
            client_notice=r.get("client_notice") or "", restock_alert=r.get("restock_alert"),
            extractor=o.extractor, extraction_confidence=o.confidence, missing_fields=",".join(o.missing),
            source_file=source or o.source_file, raw_message=raw[:20000],
            ingested_at=datetime.now().isoformat(timespec="seconds"), allocated_at=datetime.now().isoformat(timespec="seconds") if wh else None,
            stage="Allocated" if wh else "On hold", allocation_trace=json.dumps(r.get("trace")) if r.get("trace") else None,
            expected_on_time=r.get("expected_on_time"), causes=",".join(r.get("causes") or []))
        row["delivery_id"] = self.store.insert(row)
        return row

    # ---------- email ----------
    def check_mail(self):
        """Read every mailbox once. Each email is tagged with the mailbox it came from, so
        real Outlook orders and simulated ones can always be told apart."""
        found = 0; errors = {}; now = datetime.now().isoformat(timespec="seconds")
        for box in self.mailboxes:
            try: items = box.fetch()
            except Exception as e:      # one mailbox failing must not stop the others
                errors[box.name] = str(e)[:300]
                self.store.set_meta(**{f"box_{box.name}": dict(ok=False, error=str(e)[:300], checked=now, account=getattr(box, "account", None))})
                continue
            new_orders = 0
            for mid, raw in items:
                if self.store.email_seen(mid): continue
                msg = email.message_from_bytes(raw, policy=policy.default)
                o = parse_email(raw, mid, self.directory, self.names)
                if not o.received: o.received = datetime.now().strftime("%Y-%m-%d %H:%M")
                is_order = self.looks_like_order(o)
                if is_order and not o.order_ref: o.order_ref = f"ORD-{zlib.crc32(mid.encode()) % 10**8:08d}"
                self.store.mark_email(mid, is_order, str(msg.get("From", "")), str(msg.get("Subject", "")), box.name)
                if is_order:
                    try: text = raw.decode("utf-8", "replace")
                    except Exception: text = ""
                    if self.ingest("email:" + mid, "Email", o, text, msg.get("X-Halcyon-Layout"), source=box.name): new_orders += 1
                    found += 1
            self.store.set_meta(**{f"box_{box.name}": dict(ok=True, error="", checked=now, account=getattr(box, "account", None),
                                                          new_orders=new_orders, **getattr(box, "stats", {}))})
        self.store.set_meta(mailbox_errors=errors)
        if errors: raise RuntimeError("; ".join(f"{k}: {v}" for k, v in errors.items()))
        return found

    def mail_loop(self):
        while True:
            t0 = datetime.now()
            try:
                n = self.check_mail()
                acct = next((getattr(b, "account", None) for b in self.mailboxes if getattr(b, "account", None)), None)
                self.store.set_meta(watcher="ok", last_check=t0.isoformat(timespec="seconds"), last_found=n, error="",
                                    **({"mailbox": acct} if acct else {}))
            except Exception as e:
                traceback.print_exc()
                self.store.set_meta(watcher="error", last_check=t0.isoformat(timespec="seconds"), error=str(e)[:300])
            self.wake.wait(self.poll); self.wake.clear()

    # ---------- cause 1, second half: client confirms a held address, then the order is allocated ----------
    def resolve_address(self, delivery_id, street="", postal_code="", by="client"):
        rows = self.store.rows("SELECT * FROM orders WHERE delivery_id=?", (delivery_id,))
        if not rows or rows[0]["agent_status"] != "HELD_ADDRESS": return None
        row = rows[0]; raw = row["raw_message"] or ""
        ch = row["order_channel"]
        o = parse_edi(raw, row["source_file"]) if ch == "EDI" else parse_api(raw, "api") if ch == "API" else \
            parse_email(raw.encode(), row["source_file"] or "", self.directory, self.names)
        if street: o.street = street
        if postal_code: o.postal_code = postal_code
        from .parsers import _finish
        o = _finish(o)
        o.received = datetime.now().strftime("%Y-%m-%d %H:%M")   # waves are planned from the confirmation time
        if not o.missing: o.confidence = max(o.confidence, 0.95)  # the only gap was the address, now confirmed by the client
        with self.lock: r = self.agent.decide(o)
        causes = ",".join(["address_confirmed"] + [c for c in (r.get("causes") or []) if c != "address_held"])
        wh = r.get("warehouse")
        h = zlib.crc32(row["uid"].encode()) % 1000
        self.store.execute("""UPDATE orders SET agent_status=?, agent_reason=?, ship_to=?, missing_fields=?, destination_region=?,
            nearest_warehouse_id=?, fulfilment_warehouse_id=?, nearest_warehouse_had_stock=?, distance_miles=?, carrier_id=?, carrier_rate_usd=?,
            pick_list_date=?, pick_wave=?, promised_date=?, lane_on_time=?, client_notice=?, restock_alert=?, allocation_trace=?,
            expected_on_time=?, causes=?, allocated_at=?, stage=? WHERE delivery_id=?""", (
            r.get("status"), f"Address confirmed by {by}", ", ".join(x for x in [o.street, o.dock, o.city, o.state, o.postal_code] if x),
            ",".join(o.missing), o.region, r.get("nearest_warehouse"), wh, ("Y" if wh == r.get("nearest_warehouse") else "N") if wh else None,
            r.get("miles"), r.get("carrier"), round(r["carrier_rate"] * (0.92 + 0.16 * h / 1000), 2) if r.get("carrier_rate") else None,
            r.get("pick_date"), r.get("wave"), r.get("promised_date") or row["promised_date"], r.get("lane_on_time"),
            r.get("client_notice") or "", r.get("restock_alert"), json.dumps(r.get("trace")) if r.get("trace") else None,
            r.get("expected_on_time"), causes, datetime.now().isoformat(timespec="seconds") if wh else None,
            "Allocated" if wh else "On hold", delivery_id))
        return r

    def confirm_loop(self):
        """Simulated client replies to address requests (demo only: the simulator knows the full address)."""
        delay = float(os.environ.get("CONFIRM_SECONDS", "40" if os.environ.get("SIM_MODE", "realtime") == "demo" else "1200"))
        while True:
            try:
                sim = getattr(self, "sim", None)
                if sim:
                    cutoff = (datetime.now() - timedelta(seconds=delay)).isoformat(timespec="seconds")
                    for row in self.store.rows("SELECT delivery_id, order_id FROM orders WHERE agent_status='HELD_ADDRESS' AND ingested_at <= ?", (cutoff,)):
                        truth = sim.truth(row["order_id"])
                        if truth: self.resolve_address(row["delivery_id"], truth["true_street"], truth["true_postal_code"], by="client (reply to request)")
            except Exception: traceback.print_exc()
            time.sleep(10)

    def email_from_text(self, text: str):
        """An email pasted into the Order inbox screen, read exactly like one from the mailbox."""
        raw = text.replace("\r\n", "\n").encode()
        mid = f"pasted:{time.time():.6f}"
        o = parse_email(raw, "pasted email", self.directory, self.names)
        if not o.received: o.received = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = email.message_from_bytes(raw, policy=policy.default)
        is_order = self.looks_like_order(o)
        self.store.mark_email(mid, is_order, str(msg.get("From", "")), str(msg.get("Subject", "")), "pasted")
        return self.ingest("email:" + mid, "Email", o, text, source="pasted") if is_order else None

    # ---------- EDI ----------
    def edi_loop(self):
        drop = ROOT / "inbox" / "edi_drop"; done = drop / "processed"; done.mkdir(parents=True, exist_ok=True)
        while True:
            for f in sorted(drop.glob("*.edi")):
                try:
                    text = f.read_text(); o = parse_edi(text, f.name)
                    if not o.received: o.received = datetime.now().strftime("%Y-%m-%d %H:%M")
                    self.ingest("edi:" + f.name, "EDI", o, text)
                except Exception: traceback.print_exc()
                finally: f.replace(done / f.name)
            time.sleep(2)

    # ---------- API ----------
    def api_order(self, body: str):
        try: src = json.loads(body).get("source") or "api"      # "storefront" for the customer web shop
        except Exception: src = "api"
        o = parse_api(body, src)
        if not o.received: o.received = datetime.now().strftime("%Y-%m-%d %H:%M")
        return self.ingest("api:" + (o.order_ref or str(time.time())), "API", o, body)

    # ---------- tracking: a stage changes only when an event confirms it ----------
    STAGE_ORDER = {"Allocated": 1, "Picking": 2, "Shipped": 3, "Delivered": 4}

    def receive_event(self, ev: dict, via: str):
        """Apply one WMS or carrier event. Returns False if it is a duplicate or for an unknown order."""
        did, et = ev.get("delivery_id"), (ev.get("event_type") or "").upper()
        rows = self.store.rows("SELECT stage, promised_date FROM orders WHERE delivery_id=?", (did,))
        if not rows or et not in ("PICKED", "COLLECTED", "DELIVERED"): return False
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.store.execute("INSERT OR IGNORE INTO tracking_events(delivery_id, event_type, source, carrier, event_time, happened_at, received_at, detail) VALUES(?,?,?,?,?,?,?,?)",
                                 (did, et, via, ev.get("carrier"), ev.get("event_time"), ev.get("happened_at"), now, json.dumps(ev, default=str)))
        if cur.rowcount == 0: return False
        stage, promised = rows[0]["stage"], rows[0]["promised_date"]
        new = {"PICKED": "Picking", "COLLECTED": "Shipped", "DELIVERED": "Delivered"}[et]
        if self.STAGE_ORDER.get(new, 0) <= self.STAGE_ORDER.get(stage, 0) and et != "DELIVERED": return True
        if et == "PICKED":
            self.store.execute("UPDATE orders SET stage='Picking' WHERE delivery_id=?", (did,))
        elif et == "COLLECTED":
            self.store.execute("UPDATE orders SET stage='Shipped', shipped_ts=? WHERE delivery_id=?", (ev.get("event_time"), did))
            self.estimate_delivery(did, ev.get("event_time"))
        else:   # proof of delivery: Halcyon decides on-time against its own promise
            from datetime import date as _d
            dd = ev.get("delivered_date") or (ev.get("event_time") or "")[:10]
            late = (_d.fromisoformat(dd) - _d.fromisoformat(promised)).days
            self.store.execute("""UPDATE orders SET stage='Delivered', delivered_date=?, days_late=?, on_time=?, redelivery_cost_usd=0,
                shipped_ts=COALESCE(shipped_ts, ?) WHERE delivery_id=?""", (dd, late, "Y" if late <= 0 else "N", ev.get("event_time"), did))
        return True

    def estimate_delivery(self, did, collected_ts):
        """ETA from the warehouse address and the delivery address, and the order's place in the
        delivery line-up for that depot and day (see halcyon/transit.py)."""
        from .transit import road_miles, depot_arrival, route_day, delivery_time
        row = self.store.rows("SELECT fulfilment_warehouse_id wh, ship_to, destination_region region FROM orders WHERE delivery_id=?", (did,))[0]
        miles, city = road_miles(row["wh"], row["ship_to"], row["region"])
        collected = datetime.fromisoformat(collected_ts[:16])
        day = route_day(depot_arrival(collected, miles))
        route = f"{row['wh']}-{city}-{day:%Y-%m-%d}"
        stop = 1 + self.store.rows("SELECT COUNT(*) n FROM orders WHERE route_id=?", (route,))[0]["n"]
        eta = delivery_time(day, stop)
        # wall-clock time of the ETA: the same in real time; compressed in demo mode
        if os.environ.get("SIM_MODE", "realtime") == "demo":
            sec_per_h = float(os.environ.get("DEMO_SECONDS_PER_HOUR", "4"))
            eta_wall = datetime.now() + timedelta(seconds=(eta - collected).total_seconds() / 3600 * sec_per_h)
        else: eta_wall = eta
        planned_km = round(miles * 1.609, 1)
        self.store.execute("""UPDATE orders SET transit_miles=?, route_id=?, route_stop=?, eta=?, eta_wall=?, stops_on_route=?,
            planned_route_km=?, actual_route_km=? WHERE delivery_id=?""",
            (miles, route, stop, eta.strftime("%Y-%m-%d %H:%M"), eta_wall.isoformat(timespec="seconds"), stop, planned_km,
             round(planned_km * (1.0 + (zlib.crc32(did.encode()) % 12) / 100), 1), did))

    def batch_loop(self):
        """Read carrier batch files (carriers E and F) as they arrive."""
        drop = ROOT / "inbox" / "carrier_batches"; done = drop / "processed"; done.mkdir(parents=True, exist_ok=True)
        import csv
        while True:
            for f in sorted(drop.glob("*.csv")):
                try:
                    with open(f, newline="") as fh:
                        for ev in csv.DictReader(fh): self.receive_event(ev, "Carrier batch file")
                except Exception: traceback.print_exc()
                finally: f.replace(done / f.name)
            time.sleep(5)

    def start(self):
        from .order_app import OrderSimulator
        if self.agent is None: self.new_day(datetime.now().strftime("%Y-%m-%d"))
        for fn in (self.mail_loop, self.edi_loop, self.batch_loop, self.confirm_loop):
            threading.Thread(target=fn, daemon=True).start()
        if self.sim_on:
            from .tracking import CarrierSimulator
            self.sim = OrderSimulator(self); self.sim.start()
            self.carriers = CarrierSimulator(self); self.carriers.start()
