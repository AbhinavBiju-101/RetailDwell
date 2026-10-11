# RetailDwell: project context

Short, self-contained notes for resuming work in a new chat. Each section stands alone so it can be retrieved on its own.
Longer material: `presentation.md` (talking points, thresholds, data), `docs/ideation.md` (original research), `README.md` (setup).

## What it is
RetailDwell (tagline "Retail dwellscape intelligence"; earlier names: Dwell-Time & Bottleneck Mapper, Pinchlytics) is an
IBM hackathon project, problem statement 3. It turns existing CCTV into anonymous floor positions, then into heat maps,
zone dwell, journeys, queue waits and dead-zone or bottleneck flags. The lead feature is a layout experiment engine
(before/after, normalised for traffic). Plain-language flags support it. Name and tagline are `BRAND_NAME` and `BRAND_TAGLINE` in `.env`.

## Decisions already made
- Camera-only base; designed for the shopper who carries nothing. No stored video, no faces, no per-person alerts.
- Data model is four fields only: `track_id, t, x, y` (t in seconds, x and y in metres from the bottom-left of the floor).
- Spine is Branch 2 (layout experiments), support is Branch 1 (diagnostician).
- Flask with multi-page Jinja and Plotly.js from a CDN. Streamlit was rejected. Light and dark theme, concrete-grey "store survey" look, Archivo and Source Sans 3.
- Storage is Supabase (Auth, Postgres with row level security, private Storage buckets), with a local SQLite fallback for development.
- Accounts are email and password now; Google sign-in later. A fixed demo account (credentials from `.env`) is read-only and sees four simulated stores.
- The landing page is the main page, a 13-slide scrolling deck. The user wants it to reflect the ideation research.
- Real stores get API keys; camera agents POST anonymous points; CSV upload is the fallback; stores upload floor plans and draw zones.

## Run and test
- `pip install -r requirements.txt`, then `python app.py` (http://localhost:5000). With no Supabase keys it uses SQLite in `data/local` and seeds demo data on first start (local demo login `demo@dwellmapper.local` / `demo-demo-1`).
- Supabase: run `supabase/migrations/20261010000000_init.sql`, copy `.env.example` to `.env`, then `python seed_demo.py`. Turn "Confirm email" off for the demo.
- Env vars: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`, `SECRET_KEY`, `COOKIE_SECURE`, `DEMO_EMAIL`, `DEMO_PASSWORD`, `DEMO_ONE_CLICK`, `WARM`, `AUTO_SEED`, `BRAND_NAME`, `BRAND_TAGLINE`.

## File map
- `app.py`: Flask factory, auth, CSRF, routes (`/`, `/login`, `/signup`, `/app`, `/app/s/<ref>/...` (incl. `/agent` setup page), `/api/s/<ref>/...`, `POST /api/ingest/v1/tracks`, `POST /api/ingest/v1/sales`, `/healthz`), analysis cache, background warm-up.
- `pos.py`: sales (POS) matching and revenue per visitor (see Sales below). `analytics.py`: dwell, visit labels, zone stats, heat map, checkout waits (Little's law), `compare_layouts`, flags. `config.py`: thresholds.
- `stores.py` and `simulate.py`: four demo stores and the simulator. `seed_demo.py`: creates demo user and stores.
- `repo_base.py`, `repo_local.py`, `repo_supabase.py`, `repo.py`, `supa.py`: storage interface, backends, factory, REST client. `validation.py`: input checks and API key hashing.
- `templates/` (landing, agent setup, auth, store picker, overview, experiment, zones, revenue, flags, journeys, settings, zone editor), `static/` (`style.css`, `landing.css`, `app.js`, `landing.js`, favicon files).
- `agent/` (camera agent, `python -m agent`): `floor.py` homography + exclude zones, `detect.py` YOLO and motion backends,
  `tracker.py` floor-space tracker, `pipeline.py` filters and thinning, `sinks.py` CSV and ingest API (batch, retry, spool),
  `synth.py` synthetic scene + metrics, `evaluate.py` scores agent output against ground-truth floor tracks (`python -m agent evaluate`), `cli.py`. `video_to_tracks.py` is a shim for the old command. `tests/test_agent.py`: 19 tests. `tests/test_pos.py`: 14 tests (matching, flags, app flow). `tests/test_evaluate.py`: 11 tests (the scorer, by damaging a perfect estimate). `tests/test_timing.py`, `tests/test_agent_page.py`.

## Key numbers (see presentation.md for the full table)
- Stop: within 1 m for 5 s or more. Zone visit: 2 s. Heat map cells 0.5 m. Sample rate 2 Hz.
- Queue wait: Little's law; bottleneck at 4 min or more; ignore first 10 min and windows under 0.8 served per minute.
- Time-of-day baselines (`analytics.time_baselines`): 30-minute windows, a window needs 20+ shoppers and 5+ expected zone visitors; flagged when 30%+ off the zone's own average and |z| >= 3 (`config.TIME_*`). Shown on Zones (grid) and Flags ("Quieter/Busier than usual"). Needs two judgeable windows.
- Flags use 0.7x and 1.3x of the store median. Experiments use shares of shoppers with a 95% bootstrap interval; loiterers excluded; headline metric is "stopped", secondary is "reached".
- Limits: 5,000 points per ingest request; CSV 50 MB and 3 M rows; plan image 5 MB (PNG or JPEG); 40 zones per layout; passwords 8+ characters.
- Demo data: 220 to 480 visits and 45k to 119k points per layout, 0.3 to 0.8 MB compressed each, 8 recordings.

## Scoring the agent (`agent/evaluate.py`)
- `python -m agent evaluate --truth truth.csv --est tracks.csv --zones zones.json [--time-offset s] [--json out]`. Truth is `track_id,t,x,y` in metres (or an MMPTRACK folder: `--truth-format mmptrack --voxel-m ... --origin-x/--origin-y`). Zones JSON is `{"name": [x0,y0,x1,y1,"merchandise"]}`. `python -m agent selftest` now prints the same report on the synthetic scene.
- Both sides are resampled to 2 Hz, matched per bin (Hungarian, 1 m gate), then run through the app's own zone and stop code. Reports: tracking (recall, precision, position error, identity switches, tracks per person, missed people, ghost tracks); zone agreement of matched points; per person-and-zone reach/stop precision and recall and dwell error; and "as the app shows it", the stopped share per zone from the raw tracks against the truth, plus visit count / true people.
- The MMPTRACK loader follows the documented `topdown_<frame>.csv` format (id, x, y, z in a discretised grid) but is NOT checked against the real files, and `voxel_m` and the origin must come from its calibration files. Plot a few tracks first.
- Measured, synthetic scene only, motion detector, one run (180 s, 20 people, seed 5): position error median 0.10 m, p90 0.37 m; 95% of matched points in the right zone; but 3.1 agent tracks per person, visit count 1.70x the truth, dwell -36%, stopped share per zone low in every zone, 15.3 points off on average. Position is not the problem: losing people who stand still, and fragmenting them, is. The motion backend is the weak one; the YOLO path has not been scored on anything real. The earlier "finds 70 to 85% of stops" figure was true and hid this.

## Sales (POS) matching and revenue per visitor (`pos.py`)
- Input: lines `txn_id, t, zone, amount` (one row per section of a basket; `t` = seconds since the recording started, same clock as tracks). Nothing else is read: card, loyalty, cashier columns are dropped (CSV) or rejected (API). Stored per dataset as `pos/<store>/<dataset>.csv.gz` (local: `data/local/files/pos/`; Supabase: the existing private `tracks` bucket under `pos/`), so there is no schema change. Saving bumps `datasets.updated_at`, which is part of the analysis cache key.
- Method: a track's last stop (1 m, 5 s) inside a checkout zone gives its payment time. The POS/camera clock gap is estimated from the peak of all sale-to-visit time differences (within +-120 s; among near-equal comb peaks the one nearest zero wins). Sales are assigned one-to-one by Hungarian algorithm within 30 s (`config.POS_*`). Confidence = how much better a pair is than the best alternative on either side: clear >= 0.6, probable >= 0.3, else ambiguous. Ambiguous pairs are counted but excluded from revenue figures, and those shoppers are left out of the averages.
- Outputs (`analyse_pos`, attached as `R["pos"]`): revenue per shopper, share of shoppers with a matched sale (a floor, not a true conversion rate), average basket, revenue coverage, by-visit-type table, per-zone funnel (reached, stopped, bought, stoppers who buy, revenue, per shopper reached), match quality, and a revenue-per-shopper map (paths cut where the shopper joins the checkout queue; cells under 8 shoppers blank).
- Flags (`Offer problem`, `Hidden earner`, `Data check`): zone stop-to-buy rate vs the store median, needs 15+ stoppers, |z| >= 3, plus the 0.7x/1.3x ratio and a visibility condition. `Data check` fires when under 70% of sales match, or 25%+ of items were bought in zones the tracker never saw the shopper in.
- Experiment page: `compare_revenue` gives revenue per shopper before and after (whole store and the tested zone) with a bootstrap range; warns when the matched share differs by more than 10 points.
- Mocked in the demo: `simulate.baskets()` makes baskets from each buyer's true stops with its own random stream (tracks are byte-identical to before). Per-store prices and take rates are in `stores.BASKETS`; the POS clock is offset by `skew_s` with jitter, 3% of sales have no tracked shopper, 4% of items come from zones the shopper was not seen in, and the pharmacy has prescription sales that are not a floor zone. Currency symbol: `CURRENCY_SYMBOL` (default rupee).
- Measured on the simulator (ground truth known): 95.7% to 100% of usable matches go to the true shopper on all 8 layouts; clock offset recovered to within about 1 s; 1 to 24 ambiguous sales per layout (24 on the two-counter pharmacy, where tills finish together). Real tills and real tracking will be worse. Not validated on real data.
- Not built: lane-level matching (needs a till per lane zone), parties/groups, refunds, wall-clock POS timestamps (needs the recording start time, see gap 4), deleting sales data from the UI, ingest from a live POS (the API exists, no connector).

## Demo stories (simulated)
- Lakeside Grocery: offers display to the main aisle, stopped share 40% to 62%, lift +54% (+33% to +81%).
- Volt Electronics: accessories next to tills, +63% (+34% to +98%). Thread & Co.: new arrivals to the entrance, +86% (+56% to +123%).
- Corner Pharmacy: second pickup counter, median wait 3.7 to 1.3 min, peak 8.6 to 1.7; the untouched zone shows no effect.
- Revenue per shopper, before to after (whole store; tested zone): Lakeside +2% (-14% to +22%); Weekly offers +56% (+13% to +121%). Volt -0% (-22% to +29%); Accessories +22% (-5% to +63%), so traffic rose clearly and revenue from the zone did not, because the items are cheap. Thread & Co. +15% (-6% to +38%); New arrivals +145% (+57% to +300%). Pharmacy -6% (-22% to +14%), matched share 95% to 80% with two counters.
- Revenue flags: Volt Offer problem on Laptops (about 40% of shoppers stop, 19% of those buy) and Hidden earner on Accessories at the back; Thread A Offer problem on New arrivals.
- The simulator sets the effect sizes, so these show the method recovers a known effect, not real-world efficacy.

## Current state
- Done: sales (POS) matching, Revenue page and map, revenue before/after on Experiment, sales upload and `POST /api/ingest/v1/sales`, time-of-day baselines per zone, agent setup page, copy buttons, all pages, auth, demo account, store CRUD, floor plan upload, zone editor, CSV upload, API keys and ingest, heat map intensity slider and six palettes, landing deck, restyled sign-in, favicon, `presentation.md`.
- Tested on the local backend through the Flask test client (54 end-to-end checks) and with a headless browser for the landing and sign-in pages.
- Camera agent built and tested on synthetic video (median position error ~0.1 m; motion backend finds ~70 to 85% of true stops, under-counts stopped time). YOLO path only tested with a fake model. Not run on real footage.
- Not verified in the build sandbox: Docker build, gunicorn, Plotly rendering in a browser. (Supabase and Render deployment are set up and working per the user.)

## Known gaps and next steps
1. Run against a real Supabase project; fix anything the mocks missed.
2. Track stitching in `agent/pipeline.py` (short-gap re-linking) is now the most valuable agent fix: see the scorer's "as the app shows it" section. Re-run `python -m agent selftest` before and after.
3. Run the agent on a real clip with YOLO (`pip install -r requirements-video.txt`); tune `--conf`, `--min-track-s`.
4. Validate accuracy on a taped-off mock store and on MMPTRACK (`docs/validation-data.md`); replace proposed targets in `presentation.md` with measured values.
5. Google sign-in; multi-camera fusion; reseed the demo so the mock sales appear (`python seed_demo.py`, safe to repeat). Time-of-day baselines use minutes since the recording started, not clock time: add an optional recording start time (dataset metadata, not per point) to label real hours and to pool several days.
6. Landing page uses drawn illustrations, not photographs; swap in real images if wanted.
7. Make the slide deck and pitch deck from `presentation.md`.

## Working conventions
- Say what is simulated; never present demo numbers as real. Tag claims by source as in `presentation.md`.
- Keep the schema to four fields; reject extras. Service-role key stays server-side.
- UI copy: plain sentences, sentence case, no filler. Errors say what happened and how to fix it.
