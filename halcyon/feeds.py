"""Demo feeds: send the generated EDI and API orders into the live system a few at a
time, so the dashboard counts move as they would with real client feeds.
Also drops sample order emails into inbox/mail_drop/ when MAIL_MODE=folder.

Usage:  python -m halcyon.feeds --date 2026-09-28 --every 3
"""
import argparse, shutil, time, urllib.request
from pathlib import Path
from .common import ROOT

ap = argparse.ArgumentParser()
ap.add_argument("--date", default="2026-09-28"); ap.add_argument("--every", type=float, default=3)
ap.add_argument("--emails", action="store_true", help="also drop the day's email orders into inbox/mail_drop/")
ap.add_argument("--url", default="http://localhost:8000")
a = ap.parse_args()
src = ROOT / "inbox" / a.date
files = sorted([*src.glob("edi/*.edi"), *src.glob("api/*.json"), *(src.glob("email/*.eml") if a.emails else [])], key=lambda f: f.name)
(ROOT / "inbox" / "edi_drop").mkdir(parents=True, exist_ok=True); (ROOT / "inbox" / "mail_drop").mkdir(parents=True, exist_ok=True)
for f in files:
    if f.suffix == ".edi": shutil.copy(f, ROOT / "inbox" / "edi_drop" / f.name)
    elif f.suffix == ".eml": shutil.copy(f, ROOT / "inbox" / "mail_drop" / f.name)
    else:
        req = urllib.request.Request(a.url + "/api/orders", f.read_bytes(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    print("sent", f.parent.name, f.name); time.sleep(a.every)
