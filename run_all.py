"""Forecast demand, generate a day's inbox of future orders, and run the agent on it.
Usage: python run_all.py [--date 2026-09-28]"""
import argparse
from halcyon import forecast, simulate, agent

ap = argparse.ArgumentParser(); ap.add_argument("--date", default="2026-09-28")
day = ap.parse_args().date
forecast.run(42)
simulate.run(day)
agent.run(day)
