"""Forecast future order volume from the six months of delivery history.

Model: day-of-week profile x recent level.
  * Orders follow a strong weekly pattern (Friday peak, quiet weekends) and the
    weekly total has been flat at about 1,860 orders since April.
  * For each weekday we take the mean of the last LOOKBACK same weekdays.
  * Splits by channel, destination region, nearest warehouse, commitment type and
    hour come from the same lookback window.
  * An 80% interval comes from the spread of that weekday in the lookback.
The model is backtested on the last 4 weeks of history against a seasonal-naive
baseline (same weekday last week).

Usage:  python -m halcyon.forecast --days 42
"""
from __future__ import annotations
import argparse
import numpy as np
import pandas as pd
from .common import DATA, OUT, REGIONS, write_json

LOOKBACK_WEEKS = 8
Z80 = 1.2816


def daily_counts(d: pd.DataFrame) -> pd.Series:
    ts = pd.to_datetime(d.order_received_ts).dt.normalize()
    s = ts.value_counts().sort_index()
    return s.reindex(pd.date_range(s.index.min(), s.index.max()), fill_value=0)


def dow_model(history: pd.Series, horizon: pd.DatetimeIndex, lookback=LOOKBACK_WEEKS):
    recent = history[history.index > history.index.max() - pd.Timedelta(weeks=lookback)]
    prof = recent.groupby(recent.index.dayofweek).agg(["mean", "std"])
    mean = np.array([prof.loc[t.dayofweek, "mean"] for t in horizon])
    std = np.array([prof.loc[t.dayofweek, "std"] for t in horizon])
    return mean, std


def backtest(daily: pd.Series, test_weeks=4):
    cut = daily.index.max() - pd.Timedelta(weeks=test_weeks)
    train, test = daily[daily.index <= cut], daily[daily.index > cut]
    pred, _ = dow_model(train, test.index)
    naive = np.array([train.iloc[-7 + (i % 7)] for i in range(len(test))])
    mape = lambda p: float(np.mean(np.abs(p - test.values) / test.values))
    wk_err = float(abs(pred.sum() - test.sum()) / test.sum())
    return dict(test_start=str(test.index.min().date()), test_end=str(test.index.max().date()),
                model_mape=round(mape(pred), 4), naive_mape=round(mape(naive), 4), total_error=round(wk_err, 4),
                actual=[int(x) for x in test.values], predicted=[round(float(x), 1) for x in pred])


def shares(recent: pd.DataFrame, col: str) -> dict:
    return {str(k): round(float(v), 4) for k, v in recent[col].value_counts(normalize=True).items()}


def run(days=42):
    d = pd.read_csv(DATA / "deliveries.csv")
    clients = pd.read_csv(DATA / "clients.csv")
    daily = daily_counts(d)
    start = daily.index.max() + pd.Timedelta(days=1)
    horizon = pd.date_range(start, periods=days)
    mean, std = dow_model(daily, horizon)

    ts = pd.to_datetime(d.order_received_ts)
    recent = d[ts > ts.max() - pd.Timedelta(weeks=LOOKBACK_WEEKS)].copy()
    recent["hour"] = pd.to_datetime(recent.order_received_ts).dt.hour
    mix = dict(channel=shares(recent, "order_channel"), region=shares(recent, "destination_region"),
               warehouse=shares(recent, "nearest_warehouse_id"), commitment=shares(recent, "commitment_type"))
    hourly = {int(k): round(float(v), 4) for k, v in recent.hour.value_counts(normalize=True).sort_index().items()}

    rows = []
    for t, m, s in zip(horizon, mean, std):
        r = dict(date=str(t.date()), weekday=t.day_name()[:3], orders=round(float(m), 1),
                 low=round(float(max(0, m - Z80 * s)), 1), high=round(float(m + Z80 * s), 1))
        for k, v in mix["channel"].items(): r[f"ch_{k}"] = round(m * v, 1)
        for k, v in mix["warehouse"].items(): r[f"wh_{k}"] = round(m * v, 1)
        rows.append(r)
    fc = pd.DataFrame(rows)

    # Client stock placement: where each client's deliveries actually go (Q-004).
    nearest = {"Midwest": "W1/W2", "Northeast": "W4", "Southeast": "W3", "West": "W5"}
    cm = recent.groupby(["client_id", "destination_region"]).size().unstack(fill_value=0)
    per_day = cm.sum(axis=1) / recent.order_received_ts.str[:10].nunique()
    place = []
    for cid, row in cm.iterrows():
        tot = row.sum(); info = clients.set_index("client_id").loc[cid]
        split = {r: round(float(row.get(r, 0) / tot), 3) for r in REGIONS}
        place.append(dict(client_id=cid, client_name=info.client_name, home_region=info.region, tier=info.account_tier,
                          status=info.account_status, forecast_orders_4w=round(float(per_day[cid] * 28), 0),
                          out_of_home_share=round(1 - split[info.region], 3),
                          **{f"stock_{r}": split[r] for r in REGIONS}))
    place = sorted(place, key=lambda x: -x["forecast_orders_4w"])

    bt = backtest(daily)
    hist = [dict(date=str(k.date()), orders=int(v)) for k, v in daily[-56:].items()]
    OUT.mkdir(exist_ok=True)
    fc.to_csv(OUT / "forecast_daily.csv", index=False)
    pd.DataFrame(place).to_csv(OUT / "client_stock_placement.csv", index=False)
    write_json(OUT / "forecast.json", dict(model=f"day-of-week profile, {LOOKBACK_WEEKS}-week lookback",
               backtest=bt, history=hist, forecast=rows, mix=mix, hourly=hourly,
               weekly_totals=[int(x) for x in daily.resample("W-SUN").sum().values[1:-1]],
               placement=place, nearest=nearest))
    print(f"Forecast {horizon[0].date()} to {horizon[-1].date()}: {fc.orders.sum():,.0f} orders "
          f"({fc.orders.sum()/days*7:,.0f}/week). Backtest MAPE {bt['model_mape']:.1%} vs naive {bt['naive_mape']:.1%}.")
    return fc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=42)
    run(ap.parse_args().days)
