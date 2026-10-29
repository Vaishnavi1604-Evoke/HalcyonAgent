"""Delivery time estimate from the warehouse address and the delivery address.

  1. Distance  great-circle miles between the warehouse site and the delivery city, x1.2 for roads.
  2. Line-haul the truck leaves at collection and drives at 50 mph, at most 10 hours a day
               (US hours-of-service), resting 10 hours between driving days.
  3. Sort      it must reach the destination depot by 07:00 to join that morning's routes;
               otherwise it goes out the next morning. Routes run 08:00-18:00, Monday to Saturday.
  4. Line-up   on the route, the order is stop k of the shipments for that depot and day,
               in the order they were collected: 08:30 for stop 1, then 15 minutes per stop.
Warehouse sites and city coordinates are synthetic assumptions for the demo.
"""
from __future__ import annotations
import math
from datetime import datetime, timedelta

WAREHOUSE_SITES = {"W1": ("Indianapolis, IN", 39.77, -86.16), "W2": ("Joliet, IL", 41.53, -88.08),
                   "W3": ("Atlanta, GA", 33.75, -84.39), "W4": ("Allentown, PA", 40.60, -75.49),
                   "W5": ("Salt Lake City, UT", 40.76, -111.89)}
CITY = {"Chicago": (41.88, -87.63), "Columbus": (39.96, -83.00), "Indianapolis": (39.77, -86.16), "Milwaukee": (43.04, -87.91),
        "Detroit": (42.33, -83.05), "Boston": (42.36, -71.06), "Hartford": (41.76, -72.68), "Newark": (40.74, -74.17),
        "Albany": (42.65, -73.75), "Pittsburgh": (40.44, -79.99), "Atlanta": (33.75, -84.39), "Charlotte": (35.23, -80.84),
        "Nashville": (36.16, -86.78), "Orlando": (28.54, -81.38), "Columbia": (34.00, -81.03), "Denver": (39.74, -104.99),
        "Phoenix": (33.45, -112.07), "Sacramento": (38.58, -121.49), "Portland": (45.52, -122.68), "Reno": (39.53, -119.81)}
REGION_CENTRE = {"Midwest": (41.5, -87.0), "Northeast": (41.5, -74.5), "Southeast": (34.0, -84.0), "West": (39.0, -112.0)}
SPEED_MPH, DRIVE_H, REST_H, ROAD_FACTOR = 50, 10, 10, 1.2
ROUTE_START, FIRST_STOP_MIN, PER_STOP_MIN, ROUTE_END_H, SORT_BY_H = 8, 30, 15, 18, 7


def _hav(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    h = math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2
    return 3959 * 2 * math.asin(math.sqrt(h))


def city_of(ship_to: str):
    """'2210 Depot Rd, Dock 3, Nashville, TN, 37215' -> 'Nashville'"""
    parts = [p.strip() for p in (ship_to or "").split(",")]
    for i, p in enumerate(parts):
        if len(p) == 2 and p.isupper() and i > 0: return parts[i - 1]
    return ""


def road_miles(warehouse: str, ship_to: str, region: str):
    site = WAREHOUSE_SITES[warehouse]
    city = city_of(ship_to)
    dest = CITY.get(city) or REGION_CENTRE.get(region, (39, -95))
    return max(5.0, round(_hav(site[1:], dest) * ROAD_FACTOR, 0)), city or region


def _next_route_day(d):
    d = d + timedelta(days=1)
    while d.weekday() == 6: d += timedelta(days=1)   # no Sunday routes
    return d


def depot_arrival(collected: datetime, miles: float) -> datetime:
    t, left = collected, miles / SPEED_MPH
    while left > DRIVE_H:
        t += timedelta(hours=DRIVE_H + REST_H); left -= DRIVE_H
    return t + timedelta(hours=left)


def route_day(arrival: datetime):
    """The first morning whose routes this shipment can join."""
    day = arrival.replace(hour=0, minute=0, second=0, microsecond=0)
    if arrival.hour >= SORT_BY_H or day.weekday() == 6: day = _next_route_day(day)
    return day


def delivery_time(day: datetime, stop: int):
    t = day.replace(hour=ROUTE_START) + timedelta(minutes=FIRST_STOP_MIN + PER_STOP_MIN * (stop - 1))
    while t.hour >= ROUTE_END_H:   # route full: the rest of the line-up goes out the next morning
        overflow = (t - t.replace(hour=ROUTE_END_H, minute=0)).total_seconds() / 60
        day = _next_route_day(day); t = day.replace(hour=ROUTE_START) + timedelta(minutes=FIRST_STOP_MIN + overflow)
    return t
