"""Check the Outlook connection without creating any orders:  python -m halcyon.outlook_check

Prints the signed-in account, how many emails arrived in the inbox today, which of them match
ORDER_SUBJECT, and whether the agent can read an order from each match.
"""
from .live import load_env
from .outlook import OutlookMailbox
from .parsers import parse_email
from .common import client_domain, load_reference

if __name__ == "__main__":
    load_env()
    box = OutlookMailbox()
    try:
        items = box.fetch()
    except Exception as e:
        print("NOT CONNECTED:", e); raise SystemExit(1)
    clients, _, _ = load_reference()
    directory = {client_domain(n): c for c, n in zip(clients.client_id, clients.client_name)}
    names = dict(zip(clients.client_id, clients.client_name))
    s = box.stats
    print(f"Connected as {box.account}")
    print(f"Inbox today: {s['scanned_today']} emails, {s['matched_today']} with subject containing '{s['subject']}'")
    for r in s["recent_inbox"]:
        print(f"  {'MATCH' if r['matched'] else '     '}  {r['received'][:16].replace('T', ' ')}  {r['subject']}")
    for mid, raw in items:
        o = parse_email(raw, mid, directory, names)
        ok = bool(o.order_ref or (o.sku and o.qty))
        print(f"\n{mid}\n  order found: {ok}  client={o.client_id or '?'} ref={o.order_ref or '-'} item={o.sku or '?'} x {o.qty} "
              f"service={o.service or '?'} address={o.street}, {o.city}, {o.state} {o.postal_code}  missing={o.missing or 'none'}")
