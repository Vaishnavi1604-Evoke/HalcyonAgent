<<<<<<< HEAD
# Halcyon order agent

One system that takes in every order, whatever channel it came from, and assigns it to a warehouse.

- **EDI and API orders** arrive in the system automatically.
- **Email orders** are keyed in by hand today. The agent reads them instead.

## What it does

1. **Forecast** (`halcyon/forecast.py`) predicts daily orders for the next 6 weeks from six months of delivery history.
   - It uses a day-of-week profile over the last 8 weeks.
   - It splits the forecast by channel, region, warehouse and hour.
   - It recommends where each client's stock should sit, based on where their deliveries actually go.
   - Backtest on the last 4 weeks: 7.6% mean daily error, against 10.5% for a same-day-last-week baseline.
2. **Future orders** (`halcyon/simulate.py`) writes a synthetic inbox for a forecast day.
   - One file per order, in each channel's native format: X12 850 `.edi`, JSON `.json` and `.eml`.
   - It also writes opening stock per warehouse and SKU.
3. **Agent** (`halcyon/agent.py`, `halcyon/parsers.py`) reads all three channels into one record per order, then applies the checks and assignment rules:
   - Holds orders with an incomplete address before the pick list, and drafts the request to the client.
   - Sends email orders with low extraction confidence to a person for review.
   - Picks the nearest warehouse to the destination that has the stock.
   - If none in the region has it, ships from the next-nearest site, flags the order and raises a restock alert for the nearest site.
   - Picks the cheapest carrier with at least 90% on time on that lane, within a daily capacity cap.
   - Sets the pick wave (08:00, or 11:00 for wave 2) and an honest promise date, and lists the client notices to send.

## Run

```bash
pip install -r requirements.txt
python run_all.py --date 2026-09-28          # forecast + inbox + agent
# or step by step
python -m halcyon.forecast --days 42
python -m halcyon.simulate --date 2026-09-28
python -m halcyon.agent --date 2026-09-28
```

To use Claude for email extraction instead of the built-in rules, run `export ANTHROPIC_API_KEY=...` first. You can optionally choose the model with `HALCYON_MODEL`.

## Outputs (`out/`)

| File | Contents |
| --- | --- |
| `forecast_daily.csv`, `forecast.json` | Daily forecast with an 80% interval, splits by channel and warehouse, and backtest results |
| `client_stock_placement.csv` | Recommended stock split by region for each client |
| `<date>/assignments.csv` | Every order with its source, extracted fields, status, warehouse, carrier, wave and promise date |
| `<date>/exceptions.csv` | Orders held for address, held for stock, sent for review, or shipped from a fallback warehouse |
| `<date>/restock_alerts.csv` | Warehouse and SKU pairs that forced a fallback shipment |
| `<date>/client_notices.csv` | Messages to send to clients |
| `<date>/picklists/W1..W5.csv` | Pick lists by wave, with priority accounts first |
| `<date>/summary.json` | Totals, plus email extraction accuracy against the generator's ground truth |

## Assumptions to test (from the stakeholder log)

- **Wave 2 at 11:00:** a second wave can still make the 14:00–18:00 carrier collection (Q-001). Floor capacity is unknown.
- **Carrier capacity:** each carrier can take 1.3× its historical daily volume on a lane.
- **Carrier F's contract:** its volume commitment is a total, not lane-specific (Q-008).
- **Synthetic content:** all order content is synthetic. Real emails will be messier than the generated ones, so measure extraction accuracy on a labelled sample before going live.

## Live mode: dashboard counts and real-time email reading

```bash
cp .env.example .env        # set ORDER_MAILBOX and MAIL_MODE, plus credentials
python server.py            # open http://localhost:8000/login.html
```

`server.py` starts the agent and serves the UI from `ui/`. The dashboard asks `/api/summary` for new counts every 3 seconds. It shows:

- **Counts:** today's EDI, API and email orders.
- **Watcher status:** when the mailbox was last checked, and how many emails were read and how many were orders.
- **Latest orders.**

| Channel | How orders get in |
| --- | --- |
| **Email** | The watcher checks `ORDER_MAILBOX` every `POLL_SECONDS` (default 15). Each new email from today is read once and tracked by Message-ID. It is never marked as read. If the agent finds an order reference, or an item and a quantity, it counts as an order and is assigned to a warehouse. Other emails are logged and ignored. Plain-text and HTML emails are both handled. |
| **EDI** | X12 850 files dropped into `inbox/edi_drop/` are picked up within 2 seconds. In production, point the EDI translator's output folder here. |
| **API** | `POST /api/orders` with the JSON order format. |

**Connecting the real mailbox (`syeluri@evoketechnologies.com`):**

- **Microsoft 365:** use `MAIL_MODE=graph`. Microsoft usually turns off password-based IMAP, so IMAP may not work there.
  1. Register an app in Azure.
  2. Grant it the **Mail.Read** application permission, with admin consent.
  3. Limit it to this one mailbox with an Exchange application access policy.
  4. Put the tenant ID, client ID and secret in `.env`.

  This needs your IT admin.
- **Gmail or other IMAP servers:** use `MAIL_MODE=imap` with an app password.
- **Client senders:** map real sender addresses to client IDs with `CLIENT_SENDERS`. Unknown senders are still counted, but they go to review.

**Offline demo (`MAIL_MODE=folder`):** drop `.eml` files into `inbox/mail_drop/`. To replay the generated EDI and API orders, and optionally the emails, into the live system:

```bash
python -m halcyon.feeds --date 2026-09-28 --every 3 --emails
```

The replayed orders keep their 28 Sep timestamps, so their promise dates are for that day.

## Daily order simulator, database and Live orders screen

Halcyon has no live order application for the demo, so `halcyon/order_app.py` stands in for the clients' systems. It starts with the server (`SIMULATOR=on`).

**Every day it:**

1. Builds that day's order plan. Volume comes from the weekday forecast, and each order is drawn from recent orders on the same weekday, so clients, channels, service levels, destinations, arrival times and incomplete-address rates match the history.
2. Saves the plan and opening stock to `inbox/<date>/plan.json` and `inbox/<date>/inventory.json`.
3. Sends each order through its real channel:
   - **EDI:** an X12 file in `inbox/edi_drop/`.
   - **API:** the `/api/orders` logic.
   - **Email:** an `.eml` file in `inbox/mail_drop/`.

**At start-up**, it releases every order planned before the current time, so the dashboard shows a realistic count immediately. It then continues live.

**Arrival speed:**

- `SIM_MODE=realtime`: orders arrive at their planned clock time, roughly one every 3–5 minutes during the working day.
- `SIM_MODE=demo`: once caught up, a new order arrives every `DEMO_INTERVAL` seconds, with a stock top-up whenever the day's plan runs out. This is best for a hackathon demo.

If a real mailbox is configured, the simulator's emails are read alongside it, so a real test email still works during the demo.

**Each new day:**

- The agent starts fresh, with that day's stock and carrier capacity.
- Each order is allocated as it arrives, to a warehouse, a carrier, a pick wave and a promise date.
- Orders then move through Picking, Shipped and Delivered, but only when a tracking event confirms each stage. See "Tracking" below.

**Database (`out/orders.db`, table `orders`):**

- **Data dictionary columns:** every column of `deliveries.csv`, as listed in DATA_DICTIONARY.md. Shipping and delivery columns stay empty until the order gets there.
- **Agent columns:** `uid`, `client_name`, `sku`, `qty`, `ship_to`, `agent_status`, `agent_reason`, `pick_wave`, `lane_on_time`, `priority`, `client_notice`, `restock_alert`, `extractor`, `extraction_confidence`, `missing_fields`, `source_file`, `raw_message`, `ingested_at`, `allocated_at` and `stage`.
- **Delivery IDs** continue from the history: D048341 onwards.

**APIs used by the UI:**

| Endpoint | Returns |
| --- | --- |
| `GET /api/summary` | Today's counts by channel, plus simulator and email watcher status |
| `GET /api/orders?date=&channel=&warehouse=&carrier=&status=&stage=&q=&limit=&offset=` | Order list |
| `GET /api/orders/<delivery_id>` | Every column for one order, including the raw message |
| `POST /api/email-order` | Reads a pasted email exactly as if it came from the mailbox |
| `GET /api/allocation?date=` | Orders by warehouse, wave, carrier, status and stage; stock left; restock alerts |

**Screens:**

- **Dashboard:** the three channel counts.
- **Order inbox:** the same orders as the dashboard, read live from the database as they arrive.
  - The source cards show the same counts as the dashboard.
  - Select an order to see the raw message, the order record the agent built, and every decision.
  - The warehouse step lists each site with its distance and stock on hand of the SKU, and which one was chosen.
  - "Add an email order" sends a pasted email to `POST /api/email-order`, so it is saved like any other order.
- **Live orders (new menu item):**
  - Stage counts. Click one to filter.
  - Warehouse allocation, with stock left.
  - Carrier allocation and restock alerts.
  - The full order list, with filters. Click any order to see every column.

**Hackathon demo:** copy `.env.example` to `.env`, set `SIM_MODE=demo` and `DEMO_INTERVAL=5`, start the server, and open http://localhost:8000/login.html.

## Allocation rules for the five causes of lateness (target: 95% on time)

| Cause (on time when present, share of orders) | Rule the agent applies |
| --- | --- |
| **Incomplete address at order entry** (35%, 12%) | Completes the address from the client's known delivery sites (`client_sites` stands in for client master data). If more than one site matches, the order is held before the pick list, and allocated as soon as the client confirms through `POST /api/orders/<id>/address`. In the demo, the simulated client replies after `CONFIRM_SECONDS`. |
| **Same-day promised after the 8am cut-off** (36%, 10%) | Wave 2 at 11:00, then a same-day express wave at 13:00 (`EXPRESS_CAP_PER_WH` per site). Later same-day orders get an honest next-day promise at intake, and the client is told. |
| **No stock at nearest warehouse** (51%, 12%) | Stock is placed each morning from the forecast (`placed_inventory`): where orders actually go, × `SAFETY_STOCK`. In-region sites are tried first. Otherwise the order ships from the closest site with stock and a restock alert is raised. |
| **Carrier D, chosen on price** (66%, 18%) | A carrier must be at least 90% on time from the order's origin warehouse. D never is. |
| **Weak lanes: F from W3/W4, E from W5** (≈53%, 8%) | Reliability is measured per origin warehouse (`origin_lanes`), so these lanes are blocked. Among reliable carriers the most reliable wins, and those within 1 point of it compete on price. If all are full, volume surges up to 1.6× before an unreliable lane is used. |

**Expected on-time:** every order stores `expected_on_time`, the on-time rate of its lane, plus `causes`, the rules that acted on it.

**Endpoints and screens:**

- `GET /api/policy` returns today's results for each rule and the projected on-time rate.
- The Live orders screen shows this against the 95% target.
- The Order inbox explains each order's address, warehouse, carrier and wave decisions.

**Backtest:** `python -m halcyon.backtest` re-runs the 48,340 historical orders through the rules. Result: **71.0% → 95.0%**, assuming at least 98% of incomplete addresses are resolved before the pick list and 90% of nearest-site stock-outs are prevented.

- With every cause removed, the ceiling is about 95.7%. This is the on-time rate of clean orders on reliable lanes.
- Reaching 95% therefore needs the address and stock rules to work almost every time.
- Measure both in a pilot before committing.


## Tracking: how Picking, Shipped and Delivered are confirmed

A stage changes only when an event confirming it reaches Halcyon (`halcyon/tracking.py`). Every event is stored in `tracking_events`.

| Stage | Confirmed by | Source | How it reaches Halcyon |
| --- | --- | --- | --- |
| Allocated | the agent's decision | order agent | immediately |
| Picking | pick-list release scan | warehouse system | immediately |
| Shipped | carrier collection scan | carrier | through the carrier's feed |
| Delivered | proof of delivery | carrier | through the carrier's feed |

**Carrier feeds:**

- **A, B and C** push events through the API: `POST /api/tracking-events`.
- **E and F** send batch files, read from `inbox/carrier_batches/`.
- **D** offers its portal only, so it is checked periodically.

When a Delivered event arrives, Halcyon sets `delivered_date`, `days_late` and `on_time` itself, by comparing the delivery date against its own promised date.

For the demo, `CarrierSimulator` plays the warehouse system and the carriers:

- **Real-time mode:** events follow the real schedule. Picking happens at the wave time, collection in the 14:00–18:00 window, and delivery days later.
- **Demo mode:** the same sequence is compressed into minutes, using `DEMO_PICK_SECONDS`, `DEMO_SHIP_SECONDS`, `DEMO_DELIVER_SECONDS` and `DEMO_LATE_SECONDS`.

Each order's outcome (on time, or 1–3 days late) is drawn from its `expected_on_time`.


## Customer storefront (API orders)

On the login screen, choose **Customer** or **Admin**. The password for both is `halcyon2026`.

- **Customer:** a username such as `client190` signs in as client C190. Any number from 1 to 280 works, and any other username is mapped to a client. Customers land on `shop.html`:
  - **Products:** six products, each with a quantity stepper, price and **Add to cart**.
  - **Cart:** items, quantities and subtotal; a delivery address form; and a choice of Standard, Next day or Same day delivery.
  - **Submit order:** places the order with no payment step and shows a toast confirming it.
  - **My orders:** the customer's storefront orders and their status.
- **Admin:** a username such as `ops` opens the dashboard and the other admin screens. Customers cannot open admin pages.

**How storefront orders flow:**

- Each cart line is sent to `POST /api/orders` as an API order, with `source: "storefront"`.
- The agent allocates each line as its own delivery, to a warehouse and a carrier. Lines share one order reference, for example ORD-12345678-1 and ORD-12345678-2.
- These orders show in the admin dashboard's API count, the Order inbox and Live orders.

**Simulated channels:** the order simulator now creates only EDI and email orders (`SIM_CHANNELS=EDI,Email`). Add `API` to that setting to simulate API orders too.

## Email orders

For the demo, email orders are created by the order simulator and read by the agent from `inbox/mail_drop/` (`MAIL_MODE=folder`). Connecting a real Outlook mailbox is still supported in code (`MAIL_MODE=outlook`, see `halcyon/outlook.py`), but it is not shown in the app.


## Delivery time and the Delivered status

When the carrier's collection scan arrives, the agent calculates an ETA from the warehouse address and the delivery address (`halcyon/transit.py`):

1. **Road miles:** great-circle distance between the warehouse site and the delivery city, × 1.2 for roads.
2. **Line-haul:** driving at 50 mph, at most 10 hours a day, with 10-hour rests.
3. **Depot sort:** the shipment must reach the destination depot by 07:00 to go out that morning. There are no Sunday routes.
4. **Line-up:** the order becomes stop *k* on that day's route for the depot and city, in collection order. Stop 1 is at 08:30, then 15 minutes per stop. Routes end at 18:00, and any overflow goes out the next morning.

The ETA, route and stop number are stored on each order. The carrier then delivers at the ETA, unless a disruption occurs. The chance of a disruption follows the lane's measured reliability, and it adds 1–3 days.

An order turns **Delivered** only when the proof-of-delivery event arrives. On-time is judged against Halcyon's promised date.

In demo mode, each business hour of transit takes `DEMO_SECONDS_PER_HOUR` seconds (default 4).

`GET /api/deliveries` returns:

- the shipments on the road, with ETA, stop number and countdown
- how many are past their ETA
- the deliveries made today, and the on-time share

The Live orders screen shows all of this.
=======
# HalcyonAgent
>>>>>>> 1f5b3eed791058c25098caa383860e4cbd39ec72
