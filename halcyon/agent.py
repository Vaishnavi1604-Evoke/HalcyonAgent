"""Halcyon order agent: read every order from EDI, API and email, then allocate it so that
none of the five upstream causes of lateness can reach the order.

  Cause (on time when present, share of orders)        Rule applied at allocation
  1 Incomplete address at entry   (35%, 12%)    Complete it from the client's known delivery sites;
                                                if that is ambiguous, hold before the pick list and
                                                confirm with the client, then allocate.
  2 Same-day after 08:00 cut-off  (36%, 10%)    Wave 2 at 11:00 and a same-day express wave at 13:00;
                                                later same-day orders get an honest next-day promise at intake.
  3 No stock at nearest warehouse (51%, 12%)    Stock is placed each day from the forecast (where orders go,
                                                plus safety stock); in-region sites first; restock alert if not.
  4 Carrier D chosen on price     (66%, 18%)    Carrier must be >= 90% on time from that origin; D never is.
  5 Weak lanes F@W3/W4, E@W5      (~53%, 8%)    Reliability is judged per ORIGIN warehouse, so these are blocked.
     Among reliable carriers the most reliable wins; any within 1 point of it compete on price.

Warehouse and carrier choices are rules, not LLM decisions, so every one is auditable.
Usage:  python -m halcyon.agent --date 2026-09-28
"""
from __future__ import annotations
import argparse, json
from datetime import date as Date, timedelta
from pathlib import Path
import pandas as pd
from .common import (ROOT, OUT, DATA, REGIONS, WAVE1_CUTOFF_H, WAVE2_CUTOFF_H, EXPRESS_CUTOFF_H, EXPRESS_CAP_PER_WH,
                     LANE_MIN_ON_TIME, LANE_TOLERANCE, CAPACITY_FACTOR, SURGE_FACTOR, FALLBACK_ON_TIME,
                     PROMISE_DAYS, EMAIL_AUTO_ACCEPT, client_domain, client_sites, load_reference, origin_lanes, write_json)
from .parsers import parse_edi, parse_api, parse_email

ADDRESS_FIELDS = {"street", "city", "state", "postal_code", "postal_code (invalid)"}


class OrderAgent:
    def __init__(self, inventory: dict):
        clients, dist, _ = load_reference()
        self.clients = clients.set_index("client_id")
        self.directory = {client_domain(n): c for c, n in zip(clients.client_id, clients.client_name)}
        self.names = dict(zip(clients.client_id, clients.client_name))
        self.dist = dist
        lp = OUT / "lanes_origin.json"
        if not lp.exists(): write_json(lp, origin_lanes())
        self.L = json.loads(lp.read_text())
        self.express_used = {}
        self.used = {}
        self.stock = {w: dict(s) for w, s in inventory.items()}
        self.opening = {w: dict(s) for w, s in inventory.items()}

    # ---------- 1 read ----------
    def read_inbox(self, folder: Path):
        orders = []
        for f in sorted((folder / "edi").glob("*.edi")): orders.append(parse_edi(f.read_text(), f.name))
        for f in sorted((folder / "api").glob("*.json")): orders.append(parse_api(f.read_text(), f.name))
        for f in sorted((folder / "email").glob("*.eml")): orders.append(parse_email(f.read_bytes(), f.name, self.directory, self.names))
        return sorted(orders, key=lambda o: (o.received, o.source_file))

    # ---------- cause 1: complete a partial address from the client's known sites ----------
    def repair_address(self, o):
        missing = [m for m in o.missing if m in ADDRESS_FIELDS]
        if not missing or not o.client_id or not o.city: return None
        sites = [s for s in client_sites(o.client_id) if s["city"].lower() == o.city.lower() and (not o.state or s["state"] == o.state)]
        if o.street: sites = [s for s in sites if s["street"].lower() == o.street.lower()]
        if o.postal_code and "postal_code (invalid)" not in o.missing: sites = [s for s in sites if s["postal_code"] == o.postal_code]
        if len(sites) != 1: return None
        s = sites[0]; filled = []
        if not o.street: o.street = s["street"]; filled.append("street")
        if not o.postal_code or "postal_code (invalid)" in o.missing: o.postal_code = s["postal_code"]; filled.append("postal code")
        if not o.state: o.state = s["state"]; filled.append("state")
        o.missing = [m for m in o.missing if m not in ADDRESS_FIELDS]
        return filled

    # ---------- causes 4 and 5: carrier by origin reliability ----------
    def pick_carrier(self, origin, region):
        rows = []
        for c, by in self.L["origin"].items():
            lane = by.get(origin)
            if not lane: continue
            key = (c, region); vol = self.L["volume"][c][region]
            rows.append(dict(carrier=c, on_time=lane["on_time"], rate=self.L["rate"][c][region], used=self.used.get(key, 0),
                             cap=max(3, int(vol * CAPACITY_FACTOR + .999)), surge=max(4, int(vol * SURGE_FACTOR + .999)),
                             eligible=lane["on_time"] >= LANE_MIN_ON_TIME))
        elig = [r for r in rows if r["eligible"]]
        best = max((r["on_time"] for r in elig), default=0)
        top = sorted([r for r in elig if r["on_time"] >= best - LANE_TOLERANCE], key=lambda r: r["rate"])
        rest = sorted([r for r in elig if r["on_time"] < best - LANE_TOLERANCE], key=lambda r: -r["on_time"])
        order = [(r, "cap") for r in top + rest] + [(r, "surge") for r in sorted(elig, key=lambda r: -r["on_time"])]
        pick = next((r for r, lim in order if r["used"] < r[lim]), None)
        if pick is None: pick = max(rows, key=lambda r: r["on_time"])          # every reliable carrier is full
        self.used[(pick["carrier"], region)] = self.used.get((pick["carrier"], region), 0) + 1
        cheapest = min(rows, key=lambda r: r["rate"])
        avoided = [r["carrier"] for r in rows if not r["eligible"] and r["rate"] < pick["rate"]]
        return pick, rows, avoided, cheapest

    # ---------- allocate one order ----------
    def decide(self, o):
        r = dict(o.to_dict()); log = []; causes = []
        r["priority"] = bool(o.client_id in self.clients.index and (
            self.clients.loc[o.client_id, "account_tier"] == "Tier 1" or self.clients.loc[o.client_id, "account_status"] != "active"))
        # cause 1: address
        filled = self.repair_address(o)
        if filled:
            causes.append("address_repaired"); r.update(o.to_dict())
            from .parsers import STATE_REGION
            o.region = STATE_REGION.get(o.state, o.region); r["region"] = o.region
            r["address_repaired"] = filled
            if o.source == "Email":   # the fields the email lacked are now known, so confidence rises with them
                o.confidence = round(min(1.0, o.confidence + sum(f in ("street", "postal code") for f in filled) / 9), 2)
                r["confidence"] = o.confidence
            log.append(f"Address completed from client master data ({', '.join(filled)}); confirmation sent to client")
        addr_missing = [m for m in o.missing if m in ADDRESS_FIELDS]
        if addr_missing:
            causes.append("address_held")
            r.update(status="HELD_ADDRESS", reason="Missing " + ", ".join(addr_missing) + "; more than one known site matches" , causes=causes)
            log.append("Held before the pick list until the client confirms the address")
            return self._fin(r, log, notice=f"Please confirm the {' and '.join(addr_missing).replace('_', ' ')} for order {o.order_ref}.")
        if o.missing:
            r.update(status="REVIEW", reason="Missing " + ", ".join(o.missing), causes=causes); return self._fin(r, log)
        if o.source == "Email" and o.confidence < EMAIL_AUTO_ACCEPT:
            r.update(status="REVIEW", reason=f"Email extraction confidence {o.confidence:.2f}", causes=causes); return self._fin(r, log)

        # cause 3: warehouse by distance, then stock (stock itself is placed from the forecast each morning)
        order = self.dist[o.region].sort_values()
        nearest = order.index[0]
        in_region = [w for w in order.index if order[w] <= 300]
        local = sorted([w for w in in_region if self.stock[w].get(o.sku, 0) >= o.qty], key=lambda w: (-self.stock[w].get(o.sku, 0), order[w]))
        chosen = local[0] if local else next((w for w in order.index if self.stock[w].get(o.sku, 0) >= o.qty), None)
        if chosen in in_region: nearest = chosen
        r["trace"] = dict(region=o.region, sku=o.sku, qty=o.qty, in_region=in_region, chosen=chosen, address_repaired=filled or [],
            candidates=[dict(warehouse=w, miles=int(order[w]), in_region=w in in_region,
                             stock=int(self.stock[w].get(o.sku, 0)), enough=self.stock[w].get(o.sku, 0) >= o.qty) for w in order.index])
        if chosen is None:
            causes.append("stock_held")
            r.update(status="HELD_STOCK", reason=f"No warehouse holds {o.qty} x {o.sku}", causes=causes)
            return self._fin(r, log, notice=f"Order {o.order_ref} is waiting for stock; we will confirm a date.")
        self.stock[chosen][o.sku] -= o.qty
        r.update(warehouse=chosen, miles=int(order[chosen]), nearest_warehouse=nearest)
        status = "ASSIGNED"
        if chosen not in in_region:
            status = "ASSIGNED_FALLBACK"; causes.append("stock_fallback")
            r["restock_alert"] = f"{nearest}:{o.sku}"
            log.append(f"No stock in region; shipping from {chosen} ({int(order[chosen])} mi)")

        # causes 4 and 5: carrier
        pick, rows, avoided, cheapest = self.pick_carrier(chosen, o.region)
        if "D" in avoided: causes.append("carrier_d_avoided")
        if any(c in avoided for c in "EF"): causes.append("weak_lane_avoided")
        r.update(carrier=pick["carrier"], lane_on_time=pick["on_time"], carrier_rate=pick["rate"])
        r["trace"]["carriers"] = [dict(x, picked=x["carrier"] == pick["carrier"]) for x in sorted(rows, key=lambda x: -x["on_time"])]
        r["trace"]["origin"] = chosen; r["trace"]["avoided"] = avoided

        # cause 2: pick wave and honest promise
        d0 = Date.fromisoformat(o.received[:10]); h = int(o.received[11:13])
        if h < WAVE1_CUTOFF_H: wave, pick_day = "W1-08:00", d0
        elif h < WAVE2_CUTOFF_H:
            wave, pick_day = "W2-11:00", d0
            if o.service == "same_day": causes.append("cutoff_wave2")
        elif o.service == "same_day" and h < EXPRESS_CUTOFF_H and self.express_used.get(chosen, 0) < EXPRESS_CAP_PER_WH:
            wave, pick_day = "W3-13:00", d0; self.express_used[chosen] = self.express_used.get(chosen, 0) + 1
            causes.append("cutoff_express")
        else: wave, pick_day = "W1-08:00", d0 + timedelta(days=1)
        promise = d0 + timedelta(days=PROMISE_DAYS.get(o.service, 4))
        notice = None
        if o.service == "same_day" and pick_day > d0:
            promise += timedelta(days=1); r["repromised"] = True; causes.append("cutoff_repromised")
            notice = f"Order {o.order_ref} arrived after today's last same-day wave; delivery date set to {promise} at intake."
        elif status == "ASSIGNED_FALLBACK":
            notice = f"Order {o.order_ref} ships from {chosen}; delivery may take longer than usual."
        elif filled:
            notice = f"We completed the {' and '.join(filled)} for order {o.order_ref} from your site list; reply if it should go elsewhere."
        expected = FALLBACK_ON_TIME if status == "ASSIGNED_FALLBACK" else pick["on_time"]
        r.update(status=status, wave=wave, pick_date=str(pick_day), promised_date=str(promise), expected_on_time=round(expected, 4), causes=causes)
        r["trace"].update(received=o.received, wave=wave, pick_date=str(pick_day), promised=str(promise),
                          repromised=bool(r.get("repromised")), stock_after=int(self.stock[chosen][o.sku]), expected_on_time=round(expected, 4))
        return self._fin(r, log, notice=notice)

    def _fin(self, r, log, notice=None):
        r["log"] = "; ".join(log); r["client_notice"] = notice or ""
        return r

    def run(self, folder: Path):
        orders = self.read_inbox(folder)
        return [self.decide(o) for o in orders]


def evaluate(results, truth):
    """Compare email extraction with the generator's ground truth (demo only)."""
    t = {x["order_ref"]: x for x in truth}
    em = [r for r in results if r["source"] == "Email" and r["order_ref"] in t]
    keys = ["sku", "qty", "service", "street", "city", "state", "postal_code", "client_id"]
    src = lambda o, k: o.get({"street": "true_street", "postal_code": "true_postal_code"}.get(k, k), o.get(k))
    acc = {k: round(sum(str(r[k]) == str(src(t[r["order_ref"]], k)) for r in em if r.get("status") != "HELD_ADDRESS") /
                    max(1, sum(r.get("status") != "HELD_ADDRESS" for r in em)), 3) for k in keys}
    region = round(sum(r["region"] == t[r["order_ref"]]["truth_region"] for r in results if r["order_ref"] in t) / len(results), 3)
    return dict(email_orders=len(em), field_accuracy=acc, region_accuracy=region,
                unmatched=len([r for r in results if r["order_ref"] not in t]))


def run(day: str):
    folder = ROOT / "inbox" / day
    inv = json.loads((folder / "inventory.json").read_text())
    agent = OrderAgent(inv)
    res = agent.run(folder)
    out = OUT / day; (out / "picklists").mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(res)
    df.to_csv(out / "assignments.csv", index=False)
    df[~df.status.isin(["ASSIGNED"])].to_csv(out / "exceptions.csv", index=False)
    df[df.client_notice != ""][["order_ref", "client_id", "client_name", "source", "status", "client_notice"]].to_csv(out / "client_notices.csv", index=False)
    if "restock_alert" in df:
        ra = df.dropna(subset=["restock_alert"]).groupby("restock_alert").qty.agg(["count", "sum"]).reset_index()
        ra[["warehouse", "sku"]] = ra.restock_alert.str.split(":", expand=True)
        ra.rename(columns={"count": "orders_shipped_elsewhere", "sum": "units_short"})[["warehouse", "sku", "orders_shipped_elsewhere", "units_short"]].to_csv(out / "restock_alerts.csv", index=False)
    for w, g in df[df.warehouse.notna()].groupby("warehouse"):
        g.sort_values(["pick_date", "wave", "priority"], ascending=[True, True, False])[
            ["pick_date", "wave", "order_ref", "client_name", "priority", "sku", "qty", "city", "state", "carrier", "promised_date"]
        ].to_csv(out / "picklists" / f"{w}.csv", index=False)
    truth = json.loads((folder / "_truth.json").read_text()) if (folder / "_truth.json").exists() else []
    summ = dict(date=day, orders=len(df), by_source=df.source.value_counts().to_dict(), by_status=df.status.value_counts().to_dict(),
                by_warehouse=df.warehouse.value_counts().to_dict(), by_carrier=df.carrier.value_counts().to_dict(),
                email_extractor=df[df.source == "Email"].extractor.value_counts().to_dict(),
                evaluation=evaluate(res, truth) if truth else None)
    write_json(out / "summary.json", summ)
    print(json.dumps(summ, indent=2))
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--date", default="2026-09-28")
    run(ap.parse_args().date)
