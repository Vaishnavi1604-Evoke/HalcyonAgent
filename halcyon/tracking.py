"""Order tracking: how an order moves from Allocated to Picking, Shipped and Delivered.

A stage only changes when an EVENT confirming it reaches Halcyon. Nothing is moved on by
the clock alone. Every event is kept in the `tracking_events` table.

  Stage       Confirmed by                           Source                     How it reaches Halcyon
  Allocated   the agent's decision                    order agent                immediately
  Picking     pick-list release scan                  warehouse system (WMS)     immediately
  Shipped     carrier collection scan                 carrier                    by the carrier's feed (below)
  Delivered   proof of delivery (date, signature)     carrier                    by the carrier's feed (below)

Delivery time: at collection Halcyon calculates an ETA from the warehouse address and the delivery
address (distance, driving hours, depot sort time) and the order's stop number in that day's
delivery line-up (transit.py). The carrier delivers at the ETA unless a disruption delays it.

Carrier feeds (carriers.csv, delivery_event_feed):
  A, B, C   API          events pushed as they happen (POST /api/tracking-events)
  E, F      Batch file   events collected into a CSV file sent every BATCH_SECONDS
  D         Portal only  nobody is sent anything; the portal is checked every PORTAL_SECONDS
On-time is decided by Halcyon, not the carrier: delivered_date <= promised_date.

For the hackathon there are no real carriers or WMS, so CarrierSimulator plays them. It plans
each order's real-world events and releases them through the right feed.
  SIM_MODE=realtime  events follow the real schedule: pick at the wave time, collection in
                     the 14:00-18:00 window, delivery on the delivery date (days later).
  SIM_MODE=demo      the same sequence, compressed so a demo shows it in minutes:
                     picking DEMO_PICK_SECONDS after allocation, collection DEMO_SHIP_SECONDS,
                     delivery when the ETA is reached, at DEMO_SECONDS_PER_HOUR seconds per business hour.
                     Delivered dates and lateness are still business dates.
"""
from __future__ import annotations
import csv, json, os, random, threading, time, traceback, zlib
from datetime import date as Date, datetime, timedelta
from .common import ROOT

FEED = {"A": "API", "B": "API", "C": "API", "D": "Portal", "E": "Batch file", "F": "Batch file"}
WAVE_TIME = {"W1": "08:00", "W2": "11:00", "W3": "13:00"}


def _f(name, default): return float(os.environ.get(name, default))


class CarrierSimulator:
    """Plays the warehouse system and the six carriers for the demo."""

    def __init__(self, svc):
        self.svc = svc
        self.mode = os.environ.get("SIM_MODE", "realtime").lower()
        self.demo = self.mode == "demo"
        self.batch_s = _f("BATCH_SECONDS", 20 if self.demo else 1800)
        self.portal_s = _f("PORTAL_SECONDS", 45 if self.demo else 7200)
        self.dir = ROOT / "inbox" / "carrier_batches"; self.dir.mkdir(parents=True, exist_ok=True)
        self.pending = {}                       # batch / portal events waiting to be sent: carrier -> [event]
        self.last_flush = {c: time.time() for c in FEED}

    # ---------- what really happens to an order (planned once, deterministic per order) ----------
    def plan(self, row):
        """When the warehouse scans the pick and when the carrier collects. Delivery is not planned
        here: it follows the ETA Halcyon calculates at collection (transit.py), plus any disruption."""
        rnd = random.Random(zlib.crc32((row["uid"] or row["delivery_id"]).encode()))
        wave = WAVE_TIME.get((row["pick_wave"] or "")[:2], "08:00")
        ship_t = f"{14 + rnd.randint(0, 3):02d}:{rnd.randint(0, 59):02d}"
        biz = dict(picked=f"{row['pick_list_date']} {wave}", collected=f"{row['pick_list_date']} {ship_t}")
        if self.demo:
            t0 = datetime.fromisoformat(row["allocated_at"])
            due = dict(PICKED=t0 + timedelta(seconds=_f("DEMO_PICK_SECONDS", 30) + rnd.uniform(0, 10)),
                       COLLECTED=t0 + timedelta(seconds=_f("DEMO_SHIP_SECONDS", 90) + rnd.uniform(0, 30)))
        else:
            due = dict(PICKED=datetime.fromisoformat(biz["picked"]), COLLECTED=datetime.fromisoformat(biz["collected"]))
        return due, biz

    def disruption(self, row):
        """Carrier reality: most deliveries arrive at the ETA; a share set by the lane's measured
        reliability hits a disruption (missed connection, failed attempt, weather) and slips 1-3 days."""
        rnd = random.Random(zlib.crc32(("d" + (row["uid"] or row["delivery_id"])).encode()))
        late = rnd.random() >= float(row["lane_on_time"] or row["expected_on_time"] or 0.9)   # distance is already in the ETA
        return rnd.choice([1, 1, 2, 3]) if late else 0

    # ---------- one pass: release every event that has happened ----------
    def step(self):
        now = datetime.now()
        rows = self.svc.store.rows("""SELECT delivery_id, uid, carrier_id, pick_wave, pick_list_date, promised_date,
               expected_on_time, lane_on_time, allocated_at, stage, eta, eta_wall, shipped_ts FROM orders
               WHERE fulfilment_warehouse_id IS NOT NULL AND allocated_at IS NOT NULL AND stage IN ('Allocated','Picking','Shipped')""")
        if not rows: return
        sent = {(r["delivery_id"], r["event_type"]) for r in self.svc.store.rows(
            f"SELECT delivery_id, event_type FROM tracking_events WHERE delivery_id IN ({','.join('?' * len(rows))})",
            tuple(r["delivery_id"] for r in rows))}
        for p in self.pending.values(): sent |= {(e["delivery_id"], e["event_type"]) for e in p}
        for row in rows:
            due, biz = self.plan(row)
            for etype in ("PICKED", "COLLECTED", "DELIVERED"):
                if (row["delivery_id"], etype) in sent: continue
                if etype == "DELIVERED":
                    if not row["eta"]: break                     # ETA is set when the collection scan arrives
                    slip = self.disruption(row)
                    actual = datetime.fromisoformat(row["eta"]) + timedelta(days=slip)
                    wall = datetime.fromisoformat(row["eta_wall"])
                    if self.demo: wall += timedelta(seconds=slip * 24 * _f("DEMO_SECONDS_PER_HOUR", 4))
                    else: wall = actual
                    if now < wall: break
                    ev = dict(delivery_id=row["delivery_id"], event_type="DELIVERED", carrier=row["carrier_id"],
                              event_time=actual.strftime("%Y-%m-%d %H:%M"), delivered_date=actual.strftime("%Y-%m-%d"),
                              pod=random.Random(row["delivery_id"]).choice(["Signed", "Signed", "Left at dock"]),
                              delay_days=slip, happened_at=now.isoformat(timespec="seconds"))
                else:
                    if now < due[etype]: break                   # events happen in order
                    ev = dict(delivery_id=row["delivery_id"], event_type=etype, carrier=row["carrier_id"],
                              event_time=biz["picked" if etype == "PICKED" else "collected"], happened_at=now.isoformat(timespec="seconds"))
                if etype == "PICKED" or FEED.get(row["carrier_id"]) == "API":
                    self.svc.receive_event(ev, "WMS scan" if etype == "PICKED" else "Carrier API")
                else:
                    self.pending.setdefault(row["carrier_id"], []).append(ev)
                sent.add((row["delivery_id"], etype))
                break                                            # the next event waits for this one to be applied
        self.flush()

    # ---------- batch files and portal checks ----------
    def flush(self):
        for c, evs in list(self.pending.items()):
            if not evs: continue
            every = self.batch_s if FEED[c] == "Batch file" else self.portal_s
            if time.time() - self.last_flush[c] < every: continue
            if FEED[c] == "Batch file":
                path = self.dir / f"carrier_{c}_{datetime.now():%Y%m%d_%H%M%S}.csv"
                with open(path, "w", newline="") as fh:
                    cols = list(dict.fromkeys(k for e in evs for k in e))   # COLLECTED and DELIVERED rows carry different fields
                    w = csv.DictWriter(fh, fieldnames=cols, restval=""); w.writeheader(); w.writerows(evs)
            else:   # portal: Halcyon staff (or a script) look the shipments up
                for e in evs: self.svc.receive_event(e, "Carrier portal check")
            self.pending[c] = []; self.last_flush[c] = time.time()

    def loop(self):
        while True:
            try: self.step()
            except Exception: traceback.print_exc()
            time.sleep(3 if self.demo else 30)

    def start(self): threading.Thread(target=self.loop, daemon=True).start()
