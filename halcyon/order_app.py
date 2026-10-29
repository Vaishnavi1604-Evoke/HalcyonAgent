"""Order application simulator: creates each day's orders and sends them in real time.

Halcyon has no live order feed for the hackathon, so this stands in for the clients'
systems. Every day it:
  1. builds the day's order plan: volume from the weekday forecast, and each order drawn
     from recent orders on the same weekday (clients, channels, service levels,
     destinations, arrival times and incomplete-address rates match the history);
  2. saves the plan and the day's opening stock to inbox/<date>/;
  3. releases each order at its planned time, in its channel's real format:
       EDI   -> X12 850 file in inbox/edi_drop/      (picked up by the EDI watcher)
       API   -> POST to the agent's /api/orders logic
       Email -> .eml file in inbox/mail_drop/         (read by the email watcher)
On start-up it releases every order planned before the current time, so the dashboard
shows a realistic count straight away, then continues live.

Settings (.env):
  SIMULATOR=on|off      default on
  SIM_MODE=realtime     orders arrive at their planned clock time (about 1 every 3-5 min in the day)
  SIM_MODE=demo         after catching up, a new order every DEMO_INTERVAL seconds (default 5)
  SIM_CHANNELS=EDI,Email  channels the simulator creates; API orders come from the customer storefront
                          (add API to also simulate API orders)
"""
from __future__ import annotations
import json, os, random, threading, time
from datetime import datetime
from .common import ROOT, write_json
from .simulate import build_orders, placed_inventory, render_edi, render_api, render_email


class OrderSimulator:
    def __init__(self, service):
        self.svc = service
        self.mode = os.environ.get("SIM_MODE", "realtime").lower()
        self.interval = float(os.environ.get("DEMO_INTERVAL", "5"))
        self.day = None; self.plan = []; self.pos = 0; self.extra = 0
        self.channels = {c.strip() for c in os.environ.get("SIM_CHANNELS", "EDI,Email").split(",") if c.strip()}
        self.edi_dir = ROOT / "inbox" / "edi_drop"; self.mail_dir = ROOT / "inbox" / "mail_drop"
        self.edi_dir.mkdir(parents=True, exist_ok=True); self.mail_dir.mkdir(parents=True, exist_ok=True)

    # ---------- daily plan ----------
    def plan_day(self, day: str):
        folder = ROOT / "inbox" / day
        orders, row = build_orders(day)
        inv_path = folder / "inventory.json"
        inv = json.loads(inv_path.read_text()) if inv_path.exists() else placed_inventory(day)   # forecast-based placement
        write_json(inv_path, inv)
        write_json(folder / "plan.json", dict(date=day, forecast_orders=row["orders"], planned=len(orders), orders=orders))
        self.day, self.plan, self.pos, self.extra, self.sent = day, orders, 0, 0, 0
        self.svc.new_day(day, inv)
        self.svc.store.set_meta(sim_day=day, sim_planned=sum(o["channel"] in self.channels for o in orders), sim_channels=sorted(self.channels), sim_forecast=row["orders"], sim_mode=self.mode)

    def truth(self, order_id):
        """The full address behind an incomplete order (what the client would reply with)."""
        return next((o for o in self.plan if o["order_ref"] == order_id), None)

    # ---------- send one order through its channel ----------
    def send(self, o):
        if o["channel"] not in self.channels: return     # API orders now come from the customer storefront
        self.sent += 1
        name = f"{o['order_ref']}"
        if o["channel"] == "EDI":
            tmp = self.edi_dir / (name + ".tmp"); tmp.write_text(render_edi(o)); tmp.replace(self.edi_dir / (name + ".edi"))
        elif o["channel"] == "API":
            self.svc.api_order(render_api(o))
        else:
            tmp = self.mail_dir / (name + ".tmp"); tmp.write_bytes(render_email(o)); tmp.replace(self.mail_dir / (name + ".eml"))

    def _restamp(self, o, now):
        o = dict(o); o["received"] = now.strftime("%Y-%m-%d %H:%M"); o["ts"] = now.strftime("%Y-%m-%d %H:%M:%S"); return o

    def loop(self):
        caught_up = False
        while True:
            now = datetime.now(); today = now.strftime("%Y-%m-%d")
            try:
                if today != self.day:
                    self.plan_day(today); caught_up = False
                stamp = now.strftime("%Y-%m-%d %H:%M:%S")
                # release everything planned up to now (catch-up on start, then real time)
                while self.pos < len(self.plan) and self.plan[self.pos]["ts"] <= stamp:
                    self.send(self.plan[self.pos]); self.pos += 1
                if self.mode == "demo":
                    if caught_up:
                        if self.pos >= len(self.plan):   # day's plan used up: draw more orders for the demo
                            self.extra += 1
                            more, _ = build_orders(today, seed=1000 + self.extra)
                            for m in more[:50]: m["ts"] = "9999"   # released one at a time by the demo, never by the clock
                            self.plan += more[:50]
                            self.svc.replenish(0.25)             # a stock delivery so the demo can keep allocating
                        self.send(self._restamp(self.plan[self.pos], now)); self.pos += 1
                    caught_up = True
                nxt = self.plan[self.pos]["ts"] if self.pos < len(self.plan) else None
                self.svc.store.set_meta(sim_released=self.sent, sim_next=nxt if self.mode == "realtime" else f"every {self.interval:g}s",
                                        sim_status="running", sim_error="")
            except Exception as e:
                import traceback; traceback.print_exc()
                self.svc.store.set_meta(sim_status="error", sim_error=str(e)[:300])
            time.sleep(self.interval if self.mode == "demo" else 5)

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()
