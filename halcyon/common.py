"""Shared reference data and helpers for the Halcyon order agent."""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "out"

REGIONS = ["Midwest", "Northeast", "Southeast", "West"]

# Synthetic geography used for generated order content (no real customer data).
STATE_REGION = {
    **{s: "Midwest" for s in ["IL", "OH", "IN", "WI", "MI", "MN", "IA", "MO"]},
    **{s: "Northeast" for s in ["MA", "CT", "NJ", "NY", "PA", "RI"]},
    **{s: "Southeast" for s in ["GA", "NC", "TN", "FL", "SC", "AL"]},
    **{s: "West" for s in ["CO", "AZ", "CA", "OR", "WA", "NV", "UT"]},
}
CITIES = {
    "Midwest": [("Chicago", "IL", "606"), ("Columbus", "OH", "432"), ("Indianapolis", "IN", "462"), ("Milwaukee", "WI", "532"), ("Detroit", "MI", "482")],
    "Northeast": [("Boston", "MA", "021"), ("Hartford", "CT", "061"), ("Newark", "NJ", "071"), ("Albany", "NY", "122"), ("Pittsburgh", "PA", "152")],
    "Southeast": [("Atlanta", "GA", "303"), ("Charlotte", "NC", "282"), ("Nashville", "TN", "372"), ("Orlando", "FL", "328"), ("Columbia", "SC", "292")],
    "West": [("Denver", "CO", "802"), ("Phoenix", "AZ", "850"), ("Sacramento", "CA", "958"), ("Portland", "OR", "972"), ("Reno", "NV", "895")],
}
STREETS = ["Harbor Ave", "Industrial Pkwy", "Commerce Dr", "Logistics Way", "Mill St", "Riverside Blvd", "Depot Rd", "Foundry Ln"]
SKUS = ["SKU-11820", "SKU-20431", "SKU-30977", "SKU-44102", "SKU-51268", "SKU-66014"]

# Process rules agreed from the stakeholder log (Q-001, Q-002) plus team assumptions.
WAVE1_CUTOFF_H = 8      # existing 08:00 pick list
WAVE2_CUTOFF_H = 11     # ASSUMPTION: a late-morning wave can make the 14:00-18:00 collection
EXPRESS_CUTOFF_H = 13   # ASSUMPTION: a small same-day-only express wave at 13:00 still makes collection
EXPRESS_CAP_PER_WH = 30 # ASSUMPTION: express wave capacity per warehouse per day
LANE_TOLERANCE = 0.01   # carriers within 1 point of the best on-time rate count as equal; cheapest wins
SURGE_FACTOR = 1.6      # reliable carriers can be pushed to 1.6x normal volume before an unreliable lane is used
FALLBACK_ON_TIME = 0.51 # history: shipments from a warehouse outside the region are ~51% on time
SAFETY_STOCK = 1.35     # forecast-based placement: stock each site for 1.35x its expected daily demand
LANE_MIN_ON_TIME = 0.90 # carrier must be at least this reliable on the lane
CAPACITY_FACTOR = 1.3   # ASSUMPTION: a carrier can take 1.3x its historical daily lane volume
PROMISE_DAYS = {"same_day": 1, "next_day": 2, "standard": 4}  # Q-002: rule used at intake today
EMAIL_AUTO_ACCEPT = 0.85


def client_domain(client_name: str) -> str:
    return client_name.lower().replace(" ", "") + ".example"


def load_reference():
    clients = pd.read_csv(DATA / "clients.csv")
    dist = pd.read_csv(DATA / "warehouse_region_distance_miles.csv").set_index("warehouse_id")
    carriers = pd.read_csv(DATA / "carriers.csv")
    return clients, dist, carriers


def lane_table(deliveries: pd.DataFrame | None = None) -> list[dict]:
    """On-time rate of each carrier per destination region, using only clean orders
    (complete address, stock at nearest site, not a same-day order after cut-off),
    so the number reflects the carrier rather than upstream problems."""
    d = deliveries if deliveries is not None else pd.read_csv(DATA / "deliveries.csv")
    hr = pd.to_datetime(d.order_received_ts).dt.hour
    clean = d[(d.address_status == "complete") & (d.nearest_warehouse_id == d.fulfilment_warehouse_id)
              & (d.nearest_warehouse_had_stock == "Y") & ~((d.commitment_type == "same_day") & (hr >= 8))]
    days = d.order_received_ts.str[:10].nunique()
    ot = clean.assign(ot=clean.on_time.eq("Y")).groupby(["carrier_id", "destination_region"]).ot.mean()
    rate = d.groupby(["carrier_id", "destination_region"]).carrier_rate_usd.mean()
    vol = d.groupby(["carrier_id", "destination_region"]).size() / days
    return [dict(carrier=c, region=r, on_time=round(float(ot[(c, r)]), 4), rate=round(float(rate[(c, r)]), 2),
                 daily_volume=round(float(vol[(c, r)]), 1)) for (c, r) in ot.index]


def origin_lanes(deliveries: pd.DataFrame | None = None) -> dict:
    """Carrier reliability by ORIGIN warehouse (the image's weak lanes are F from W3/W4, E from W5,
    and D from everywhere), measured on clean orders shipped from the nearest site.
    Rates and daily volumes stay per destination region."""
    d = deliveries if deliveries is not None else pd.read_csv(DATA / "deliveries.csv")
    hr = pd.to_datetime(d.order_received_ts).dt.hour
    clean = d[(d.address_status == "complete") & (d.nearest_warehouse_id == d.fulfilment_warehouse_id)
              & (d.nearest_warehouse_had_stock == "Y") & ~((d.commitment_type == "same_day") & (hr >= 8))]
    ot = clean.assign(ot=clean.on_time.eq("Y")).groupby(["carrier_id", "fulfilment_warehouse_id"]).ot.agg(["mean", "size"])
    days = d.order_received_ts.str[:10].nunique()
    rate = d.groupby(["carrier_id", "destination_region"]).carrier_rate_usd.mean()
    vol = d.groupby(["carrier_id", "destination_region"]).size() / days
    return dict(
        origin={c: {w: dict(on_time=round(float(ot.loc[(c, w), "mean"]), 4), n=int(ot.loc[(c, w), "size"]))
                    for w in ["W1", "W2", "W3", "W4", "W5"] if (c, w) in ot.index} for c in "ABCDEF"},
        rate={c: {r: round(float(rate[(c, r)]), 2) for r in REGIONS} for c in "ABCDEF"},
        volume={c: {r: round(float(vol[(c, r)]), 1) for r in REGIONS} for c in "ABCDEF"})


def client_sites(client_id: str, home_region: str | None = None) -> list[dict]:
    """Client master data: each client's known delivery sites (synthetic, stable per client).
    The agent uses it to complete partial addresses; some clients have two sites in one city,
    which the agent cannot resolve on its own."""
    import random
    rng = random.Random("sites-" + client_id)
    sites = []
    for region in REGIONS:
        cities = CITIES[region]
        for _ in range(rng.choice([1, 2, 2, 3])):
            city, st, zp = rng.choice(cities)
            sites.append(dict(region=region, city=city, state=st, street=f"{rng.randint(100, 8999)} {rng.choice(STREETS)}",
                              dock=f"Dock {rng.randint(1, 9)}", postal_code=zp + f"{rng.randint(0, 99):02d}"))
    return sites


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))
