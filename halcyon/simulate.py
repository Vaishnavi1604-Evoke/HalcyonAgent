"""Generate a synthetic inbox of future orders for one forecast day.

Volume comes from the forecast. Each order is drawn from recent orders placed on the
same weekday, so the mix of clients, channels, service levels, destinations, arrival
times and incomplete-address rates matches history. Raw messages are written in each
channel's native format:
    inbox/<date>/edi/*.edi     X12 850 purchase orders (arrive automatically)
    inbox/<date>/api/*.json    API submissions (arrive automatically)
    inbox/<date>/email/*.eml   email orders (keyed by hand today; read by the agent)
    inbox/<date>/inventory.json  opening stock per warehouse and SKU

All content is synthetic (hackathon rule: no real customer data).
Usage:  python -m halcyon.simulate --date 2026-09-28
"""
from __future__ import annotations
import argparse, json, os, random
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime
import pandas as pd
from .common import DATA, OUT, ROOT, CITIES, STREETS, SKUS, SAFETY_STOCK, client_domain, client_sites, write_json

TEMPLATES = [
    "Please send {qty} units of {sku} to our {city} site ({addr}). We need this {svc}. PO {po}.",
    "PO: {po}\nItem: {sku} x {qty}\nDeliver to: {addr}\nService: {svc}",
    "Could you arrange {svc} delivery of {sku} (qty {qty})? Ship to {addr}. Our reference is {po}.",
    "Order {po} below:\n{sku} | {qty} EA\nAddress - {addr}\nPriority: {svc}",
]
SVC = {"same_day": "same day", "next_day": "next day", "standard": "standard"}


def build_orders(date: str, seed=7):
    rng = random.Random(seed + int(date.replace("-", "")))
    d = pd.read_csv(DATA / "deliveries.csv")
    row = None
    fp = OUT / "forecast.json"
    if fp.exists():
        row = next((r for r in json.loads(fp.read_text())["forecast"] if r["date"] == date), None)
    if row is None:  # outside the saved forecast: use the same day-of-week model directly
        from .forecast import daily_counts, dow_model
        m, s = dow_model(daily_counts(d), pd.DatetimeIndex([pd.Timestamp(date)]))
        row = dict(date=date, orders=round(float(m[0]), 1))
    n = max(1, int(round(rng.gauss(row["orders"], row["orders"] * 0.04))))
    clients = pd.read_csv(DATA / "clients.csv").set_index("client_id")
    ts = pd.to_datetime(d.order_received_ts)
    dow = pd.Timestamp(date).dayofweek
    pool = d[(ts > ts.max() - pd.Timedelta(weeks=8)) & (ts.dt.dayofweek == dow)]
    sample = pool.sample(n=n, replace=True, random_state=rng.randint(0, 10**6))
    orders = []
    for i, r in enumerate(sample.itertuples()):
        site = rng.choice([s for s in client_sites(r.client_id) if s["region"] == r.destination_region])
        city, st, street, zipc = site["city"], site["state"], site["street"], site["postal_code"]
        missing = []
        if r.address_status == "incomplete":
            missing = rng.choice([["postal_code"], ["street"], ["street", "postal_code"]])
        hhmm = r.order_received_ts[11:16]
        orders.append(dict(
            order_ref=f"ORD-{rng.randint(10**7, 10**8 - 1)}", client_id=r.client_id,
            client_name=clients.loc[r.client_id, "client_name"], channel=r.order_channel,
            email_layout=int(r.email_format_id) if r.email_format_id == r.email_format_id else 0,
            received=f"{date} {hhmm}", ts=f"{date} {hhmm}:{rng.randint(0, 59):02d}", service=r.commitment_type, sku=rng.choice(SKUS), qty=rng.randint(6, 45),
            street="" if "street" in missing else street, dock=site["dock"], city=city, state=st,
            postal_code="" if "postal_code" in missing else zipc, truth_region=r.destination_region,
            true_street=street, true_postal_code=zipc))
    return sorted(orders, key=lambda o: o["ts"]), row


def write_edi(o, path): path.write_text(render_edi(o))


def render_edi(o) -> str:
    t = o["received"][11:].replace(":", "")
    segs = [f"ISA*00*          *00*          *ZZ*{o['client_id']:<15}*ZZ*HALCYON        *{o['received'][2:10].replace('-', '')}*{t}*U*00401*000000001*0*P*>",
            "ST*850*0001", f"BEG*00*SA*{o['order_ref']}**{o['received'][:10].replace('-', '')}",
            f"TD5**2*{o['service'].upper()}", f"N1*ST*{o['client_name']}*92*{o['client_id']}",
            f"N3*{o['street']}*{o['dock']}" if o["street"] else f"N3*{o['dock']}",
            f"N4*{o['city']}*{o['state']}*{o['postal_code']}", f"PO1*1*{o['qty']}*EA***VP*{o['sku']}", "SE*8*0001"]
    return "~\n".join(segs) + "~\n"


def write_api(o, path): path.write_text(render_api(o))


def render_api(o) -> str:
    return (json.dumps(dict(order_ref=o["order_ref"], client_id=o["client_id"], service_level=o["service"],
        ship_to=dict(name=o["client_name"], line1=o["street"] or None, line2=o["dock"], city=o["city"], state=o["state"],
                     postal_code=o["postal_code"] or None),
        lines=[dict(sku=o["sku"], qty=o["qty"])], submitted_at=o["received"].replace(" ", "T") + ":00"), indent=2))


def write_email(o, path): path.write_bytes(render_email(o))


def render_email(o) -> bytes:
    addr = ", ".join(x for x in [o["street"], o["dock"], f"{o['city']}, {o['state']}", o["postal_code"]] if x)
    body = TEMPLATES[o["email_layout"] % 4].format(qty=o["qty"], sku=o["sku"], city=o["city"], addr=addr,
                                                     svc=SVC[o["service"]], po=o["order_ref"])
    m = EmailMessage()
    m["From"] = f"Purchasing <orders@{client_domain(o['client_name'])}>"
    m["To"] = os.environ.get("ORDER_MAILBOX", "orders@halcyon.example")
    m["X-Halcyon-Layout"] = str(o["email_layout"])
    m["Subject"] = f"Order {o['order_ref']}"
    m["Date"] = format_datetime(datetime.strptime(o["received"], "%Y-%m-%d %H:%M"))
    m.set_content(f"Hi team,\n\n{body}\n\nThanks,\n{o['client_name']} purchasing\n")
    return bytes(m)


def placed_inventory(day: str, rng=None):
    """Forecast-based stock placement (fix for cause 3): stock each warehouse for its expected
    demand that weekday x SAFETY_STOCK, using where orders actually go, not the client's home region.
    Midwest demand is split between W1 and W2. Built from the forecast only, never from the day's orders."""
    d = pd.read_csv(DATA / "deliveries.csv")
    ts = pd.to_datetime(d.order_received_ts)
    recent = d[ts > ts.max() - pd.Timedelta(weeks=8)]
    dow = pd.Timestamp(day).dayofweek
    same = recent[pd.to_datetime(recent.order_received_ts).dt.dayofweek == dow]
    per_day = same.groupby("destination_region").size() / same.order_received_ts.str[:10].nunique()
    near = {"Midwest": ["W1", "W2"], "Northeast": ["W4"], "Southeast": ["W3"], "West": ["W5"]}
    mean_qty = 25.5
    inv = {w: {} for w in ["W1", "W2", "W3", "W4", "W5"]}
    for region, n in per_day.items():
        for w in near[region]:
            for s in SKUS:
                inv[w][s] = inv[w].get(s, 0) + int(round(n / len(near[region]) / len(SKUS) * mean_qty * SAFETY_STOCK)) + 30
    return inv


def opening_inventory(orders, rng):
    """Stock per warehouse/SKU set around expected demand, some SKUs short, so
    stock-outs at the nearest site happen at roughly the historical 12% rate."""
    near = {"Midwest": ["W1", "W2"], "Northeast": ["W4"], "Southeast": ["W3"], "West": ["W5"]}
    demand = {}
    for o in orders:
        ws = near[o["truth_region"]]
        for w in ws:
            demand[(w, o["sku"])] = demand.get((w, o["sku"]), 0) + o["qty"] / len(ws)
    inv = {w: {} for w in ["W1", "W2", "W3", "W4", "W5"]}
    for w in inv:
        for s in SKUS:
            exp = demand.get((w, s), 0)
            factor = rng.choice([0.55, 0.8, 1.2, 1.5, 2.0, 2.5])
            inv[w][s] = int(round(exp * factor)) + 40
    return inv


def run(date: str):
    orders, row = build_orders(date)
    base = ROOT / "inbox" / date
    for sub in ["edi", "api", "email"]: (base / sub).mkdir(parents=True, exist_ok=True)
    for i, o in enumerate(orders):
        stem = f"{i+1:04d}_{o['order_ref']}"
        {"EDI": lambda: write_edi(o, base / "edi" / f"{stem}.edi"),
         "API": lambda: write_api(o, base / "api" / f"{stem}.json"),
         "Email": lambda: write_email(o, base / "email" / f"{stem}.eml")}[o["channel"]]()
    inv = opening_inventory(orders, random.Random(int(date.replace("-", ""))))
    write_json(base / "inventory.json", inv)
    write_json(base / "_truth.json", orders)  # for evaluating the agent only; the agent never reads it
    by = pd.Series([o["channel"] for o in orders]).value_counts().to_dict()
    print(f"{date}: {len(orders)} orders written to {base} (forecast {row['orders']}) {by}")
    return orders


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--date", default="2026-09-28")
    run(ap.parse_args().date)
