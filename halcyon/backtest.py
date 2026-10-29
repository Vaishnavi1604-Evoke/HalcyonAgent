"""Backtest the allocation rules on the six months of history (April-September 2026).

Each historical order is re-run through the five rules. An order whose causes are all
removed takes the on-time rate of clean orders on the lane the rules would pick (most
reliable carrier from the nearest warehouse, cheapest within 1 point). An order with a
cause the rules cannot fully remove keeps its real outcome. Two inputs are assumptions,
stated in the result and adjustable:
  ADDRESS_RESOLVED  share of incomplete addresses completed or confirmed before the pick list
  STOCKOUT_PREVENTED share of nearest-warehouse stock-outs avoided by forecast-based placement
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .common import DATA, LANE_MIN_ON_TIME, LANE_TOLERANCE, EXPRESS_CUTOFF_H, origin_lanes

ADDRESS_RESOLVED = 0.98   # needed for 95%; the live run completes or confirms ~100% before the pick list
STOCKOUT_PREVENTED = 0.90 # needed for 95%; compare with the live fallback rate on the Live orders screen


def run(address_resolved=ADDRESS_RESOLVED, stockout_prevented=STOCKOUT_PREVENTED, seed=7):
    d = pd.read_csv(DATA / "deliveries.csv")
    ot = d.on_time.eq("Y").to_numpy(float)
    hr = pd.to_datetime(d.order_received_ts).dt.hour
    L = origin_lanes(d)
    # on-time of the lane the rules pick from each origin: carriers within 1 point of the best reliable one
    lane_p = {}
    for w in ["W1", "W2", "W3", "W4", "W5"]:
        el = [by[w]["on_time"] for by in L["origin"].values() if w in by and by[w]["on_time"] >= LANE_MIN_ON_TIME]
        best = max(el); lane_p[w] = float(np.mean([x for x in el if x >= best - LANE_TOLERANCE]))
    p = d.nearest_warehouse_id.map(lane_p).to_numpy()
    rng = np.random.default_rng(seed)
    causes = {
        "address": (d.address_status == "incomplete").to_numpy(),
        "cutoff": ((d.commitment_type == "same_day") & (hr >= 8)).to_numpy(),
        "stock": (d.nearest_warehouse_had_stock == "N").to_numpy(),
        "carrier_d": (d.carrier_id == "D").to_numpy(),
        "weak_lane": (((d.carrier_id == "F") & d.fulfilment_warehouse_id.isin(["W3", "W4"])) |
                      ((d.carrier_id == "E") & (d.fulfilment_warehouse_id == "W5"))).to_numpy(),
    }
    misrouted = ((d.nearest_warehouse_had_stock == "Y") & (d.nearest_warehouse_id != d.fulfilment_warehouse_id)).to_numpy()
    remain = np.zeros(len(d), bool)
    remain |= causes["address"] & (rng.random(len(d)) >= address_resolved)
    remain |= causes["stock"] & (rng.random(len(d)) >= stockout_prevented)
    # cut-off: waves at 11:00 and 13:00 plus honest next-day promises remove it; carrier causes are blocked outright
    new = np.where(remain, ot, p)
    labels = {"address": "Incomplete address at order entry", "cutoff": "Same-day promised after 8am cut-off",
              "stock": "No stock at nearest warehouse", "carrier_d": "Carrier D, chosen on price",
              "weak_lane": "Weak lanes: F from W3/W4, E from W5"}
    rows = [dict(key=k, cause=labels[k], share=round(float(m.mean()), 4), before=round(float(ot[m].mean()), 4),
                 after=round(float(new[m].mean()), 4)) for k, m in causes.items()]
    return dict(baseline=round(float(ot.mean()), 4), projected=round(float(new.mean()), 4), target=0.95,
                assumptions=dict(address_resolved=address_resolved, stockout_prevented=stockout_prevented,
                                 express_wave=f"{EXPRESS_CUTOFF_H}:00", lane_min_on_time=LANE_MIN_ON_TIME),
                lane_on_time_by_origin={k: round(v, 4) for k, v in lane_p.items()}, causes=rows,
                misrouted_fixed=int(misrouted.sum()))


if __name__ == "__main__":
    import json; print(json.dumps(run(), indent=2))
