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
- Env vars: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`, `SECRET_KEY`, `COOKIE_SECURE`, `DEMO_EMAIL`, `DEMO_PASSWORD`, `DEMO_ONE_CLICK`, `WARM`, `AUTO_SEED`, `BRAND_NAME`, `BRAND_TAGLINE`, `CURRENCY_SYMBOL`, `GOOGLE_SIGNIN`, `APP_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_API`, `BRIEF_DAILY_LIMIT`, `BRIEF_MAX_INPUT_TOKENS`, `BRIEF_MAX_OUTPUT_TOKENS`, `BRIEF_AI_FOR_DEMO`.
- Migrations to run on Supabase, in order: `20261010000000_init.sql`, then `20261011000000_start_time.sql`, then `20261012000000_settings.sql` (adds `stores.settings`; without it detection settings cannot be saved). The start-time migration (adds `stores.timezone` and `datasets.started_at`; the app still runs without it but cannot save a start time or timezone). A local SQLite database upgrades itself on start. `requirements.txt` now includes scipy (sales matching, party and agent tracking) and tzdata (timezones on Windows).

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
- Sales `t` may be seconds since the recording started, or real timestamps (local time in the store timezone, with a UTC offset, or epoch seconds/milliseconds) once the recording has a start time; lines from before the start are dropped and reported.
- Parties: people who arrive together buy together. A party is one register visit (lowest track id, mean end time), a companion of a payer counts as having bought, revenue is counted once, and Revenue shows revenue per party next to per shopper. Measured on the pharmacy simulation: conversion 0.75 with parties handled against 0.52 without (0.74 with no companions at all), and 28 ambiguous sales against 0.
- Not built: lane-level matching (needs a till per lane zone), refunds, deleting sales data from the UI, ingest from a live POS (the API exists, no connector).

## Demo stories (simulated)
- Lakeside Grocery: offers display to the main aisle, stopped share 40% to 62%, lift +54% (+33% to +81%).
- Volt Electronics: accessories next to tills, +63% (+34% to +98%). Thread & Co.: new arrivals to the entrance, +86% (+56% to +123%).
- Corner Pharmacy: second pickup counter, median wait 3.7 to 1.3 min, peak 8.6 to 1.7; the untouched zone shows no effect.
- Revenue per shopper, before to after (whole store; tested zone): Lakeside +2% (-14% to +22%); Weekly offers +56% (+13% to +121%). Volt -0% (-22% to +29%); Accessories +22% (-5% to +63%), so traffic rose clearly and revenue from the zone did not, because the items are cheap. Thread & Co. +15% (-6% to +38%); New arrivals +145% (+57% to +300%). Pharmacy -10% (-28% to +12%), matched share 96% to 74% with two counters (this changed from -6% when companions and staff were added to its simulation; the other three stores did not move).
- Revenue flags: Volt Offer problem on Laptops (about 40% of shoppers stop, 19% of those buy) and Hidden earner on Accessories at the back; Thread A Offer problem on New arrivals.
- The simulator sets the effect sizes, so these show the method recovers a known effect, not real-world efficacy.

## Recording start time and wall-clock labels (`clock.py`)
- `datasets.started_at` (UTC) and `stores.timezone` (IANA name; default UTC; demo stores use Asia/Kolkata). Set in Settings (store timezone, a Start time form per recording, and an optional field on the recording upload), or sent by the agent: `ApiSink` adds `started_at` to its batches and the server keeps the first one it hears. The agent sends it automatically for live sources (camera index, rtsp, http), or from `--started-at 2026-10-10T14:00+05:30`; a video file has no known start so none is sent. Saving bumps `updated_at`, so cached analyses rebuild.
- With a start time, time-of-day windows read "14:00 to 14:30" and flag text says "between 14:05 and 14:10 on Sat 10 Oct"; without one, nothing changes (minutes since the start). Windows are still 30 minutes (`TIME_WINDOW_S`).
- Pooling several days: see Pooling below.

## Per-store detection settings (`thresholds.py`)
- One table of 26 settings (default from `config.py`, range, plain-language help, and whether it applies to the server or the agent), stored per store in `stores.settings` (only values that differ from the default). Edited under Settings, Detection settings. Groups: Staff, Parties, Several cameras (fusion), Track stitching (camera agent), Sales matching. Invalid values are refused with the field named; a bad stored value is ignored on read and can never break a page.
- Staff, party, fusion and sales settings are read by the server on every analysis and are part of the cache key, so a change re-analyses existing recordings at once. Stitching settings run in the agent: `GET /api/ingest/v1/config` (API key) returns only the stitching keys, and `python -m agent run --api ...` fetches them at start (`--ignore-server-settings` skips it; if the server cannot be reached the built-in defaults are used with a warning; `--no-stitch` always wins). They apply to the next agent run, not to points already sent.
- Settings are per store, not per layout, so a before/after experiment is always judged by the same rules.
- Not exposed: dwell radius and duration, time-of-day and flag thresholds (they define the product's numbers and are used across many modules).

## Pooling several days (`pooling.py`)
- A layout can already hold many recordings (newest is shown by default). Every page takes `?rec=latest|all|<recording id>` (a selector appears when there are two or more; API calls from the pages carry it). `all` pools up to the newest 14 recordings; one recording pooled is just that recording; a stale or foreign id falls back to latest. The experiment page honours `rec=all` for both layouts.
- Method: each recording is fused (if multi-camera), shifted in time so days never overlap, and given dense unique ids; sales are pooled with the same shift (one clock correction for all days). With a start time on every recording, time-of-day windows become clock hours that gather shoppers from all days, and checkout flags name the hour; without one the hour-by-hour findings are switched off with a reason (everything else still pools).
- Verified: pooled visits and shoppers equal the sum of the days exactly, no cross-day parties, pooled reach is the shopper-weighted average, pooled sales match like the days separately (within 3%), and the experiment's likely range narrows when both layouts use all days.
- Limits: weekdays and weekends are pooled as they are (no per-recording include flag); the hour test treats shoppers as independent, so a few unusual days can move a pooled hour more than the test suggests; no leave-one-day-out "usual for this hour" flag; recordings are listed and deletable under Settings (a delete removes the tracks and sales files).
- Fixed on the way: local `list_datasets` now breaks same-second ties by insertion order (latest was arbitrary for two recordings saved in one second).

## Several cameras on one floor (`fusion.py`)
- Setup: one agent per camera, each with its own calibration to the same floor coordinates, same `--layout` and `--recording`, and `--camera N` (0 to 31; camera 0 is exactly the old behaviour). The camera number is stored in the track id's upper bits (`id >> 48`), so there is no schema change and old agents are camera 0. The agent's default id base is 40 random bits, which is why the number cannot be inferred from id ranges.
- Server side, per recording before any pooling: tracks from different cameras that stay within `fuse_dist_m` (0.8 m) for `fuse_frac` (70%) of at least `fuse_min_overlap_s` (1 s) are one person (positions averaged); a track that starts within `handoff_gap_s` (4 s) of another camera's track ending, within 0.8 m plus 1.2 m/s of where it ended, continues it. Same-camera tracks are never joined and two same-camera tracks alive at once are never put in one person. Overview says how many cameras, how many duplicates and hand-overs, and how far apart the cameras place the same person (warns above 60% of the limit): that gap is the number to watch when checking cameras' calibration against each other.
- Measured on the simulator (`simulate.cameras`: floor split along its length with a 3 m overlap, 0.15 m noise, 0.15 m per-camera calibration bias, 8% frame dropout, id break after 1.5 s lost). Settings tuned on seeds 5 to 7 (3 seeds, 2 and 3 cameras, lakeside A, 350 people); held-out seed 9, 2 cameras: visit count 2.97x the truth raw, 1.21x fused; stopped-share error 19.9 to 5.4 points; 2 wrong clusters (two people in one) of 423. Across the tuning seeds the visit ratio was about 1.4x (2 cameras) and 1.9x (3 cameras) fused, with 1 to 3% wrong merges; a single noisy camera is 1.13x, so fusion does not fully reach the single-camera floor. The first defaults (3 s minimum overlap, 8 s hand-over) gave 2.0x and 9% wrong merges: a fast walker crosses a 3 m overlap in about 2 s. Tighter distance (0.6 m) or share (0.85) made it worse because calibration bias alone is about 0.35 m.
- Limits: drawn people and an idealised camera model (a straight split, constant bias); real cameras have lens distortion, calibration that drifts, and people hidden behind each other. Two people inside the calibration error of each other for several seconds in an overlap can be merged. Gaps in coverage longer than the hand-over gap are not bridged. Fusion costs 2 to 4 s per recording on first analysis (then cached). Not tested on any real multi-camera footage.

## Staff and parties (`analytics.find_staff`, `find_parties`)
- Staff are left out of every figure and listed with the reason on Overview. Rules (config `STAFF_*`): 60 s in a zone of type `staff` (new zone type, grey in the editor), 10 minutes standing in a checkout zone, or 40 minutes on the floor when the recording is at least twice that. Measured on the pharmacy simulation: both staff found, none wrongly. An always-present cashier otherwise adds about +1 to checkout occupancy and inflates the estimated wait by roughly 0.5 to 0.9 minutes.
- Parties (config `PARTY_*`): within 1.5 m for 40 s and 60% of the shorter visit, with 10 s of walking, and appearing and disappearing within 8 s of each other (the last condition is what stops people queueing side by side being joined). Measured on the pharmacy simulation: recall 0.96 to 0.97, precision 1.00 on both layouts; on the three stores with no companions, 0 or 1 false party per 300 to 480 visits. Real families will drift apart more than the simulated ones and fragmented tracks will lower recall.

## Track stitching (`agent/pipeline.py`)
- A new detector track that starts within 8 s of an established track's last sighting, within 0.6 m plus 1.2 m/s of where it was, takes over that identity; never when the old track was last seen at the floor edge (it left), never while the old one is still being seen, and an old id that returns becomes its own track. `--no-stitch` turns it off.
- Sweep on the synthetic scene with the motion backend (4 seeds, 20 people, 150 s): visit count 1.90x the truth without it, 0.98x with the chosen settings; stopped share error 17.6 to 6.1 points; dwell bias -33% to -9%. A longer 20 s gap fixed dwell better (+2%) but pushed the visit count to 0.81x. Tracks that merge two people stayed at about 10.8 in every variant, so stitching did not add wrong merges here. The settings are tuned on drawn people and the motion detector; retune on real footage (`stitch_*` in `Pipeline`).

## Store-manager brief (`brief.py`)
- Page `/app/s/<ref>/brief`. A deterministic template summary is always shown; with `GEMINI_API_KEY` set, a writer can press a button to have Gemini write it instead. Page loads never call Gemini.
- Sent to Gemini: aggregate facts only (zone reach and stop percentages, KPIs, findings text, revenue and experiment figures). Not sent: tracks, sales lines, identities. A test asserts the facts hold none of those fields.
- Default API is `generateContent` with `x-goog-api-key`, model `gemini-3.8-flash`, `maxOutputTokens` 2048; `GEMINI_API=interactions` uses the newer Interactions API (`store: false`). Both response shapes are parsed. NOT tested against the live API (no key here): tests use fake responses built from the documentation.
- Falls back to the template with a plain warning when: key rejected, 403, unknown model, 429/quota (not retried), 5xx or network failure (retried once), blocked or cut-off answer (`MAX_TOKENS` included), empty answer, facts too large (trimmed, then refused above `BRIEF_MAX_INPUT_TOKENS`), the per-store daily cap (`BRIEF_DAILY_LIMIT`, counted per process, failed calls count), or text containing more than two figures that are not in the facts. Demo stores use the template unless `BRIEF_AI_FOR_DEMO=1`.

## Google sign-in
- Off unless `GOOGLE_SIGNIN=1` and the backend is Supabase. `/auth/google` starts PKCE (verifier kept in the browser session, challenge is S256), `/auth/callback` exchanges `?code=` at `/auth/v1/token?grant_type=pkce`. A callback this browser did not start is refused; `next` is checked against open redirects. New Google users get an owner profile from the existing trigger.
- Setup (nothing is in the code): Google Cloud OAuth client of type Web application with the authorised redirect URI `https://<project>.supabase.co/auth/v1/callback`; enable the Google provider in the Supabase dashboard with that client ID and secret; add `https://<your app>/auth/callback` to Supabase's allowed redirect URLs and set the Site URL; set `GOOGLE_SIGNIN=1` (and `APP_URL` if the app is behind a proxy that hides its public address). Not tested against real Google or Supabase.

## Current state
- Done: track stitching, recording start time and wall-clock labels, staff filtering, parties, store-manager brief (template and Gemini), Google sign-in (off until configured), the agent scorer, sales (POS) matching, Revenue page and map, revenue before/after on Experiment, sales upload and `POST /api/ingest/v1/sales`, time-of-day baselines per zone, agent setup page, copy buttons, all pages, auth, demo account, store CRUD, floor plan upload, zone editor, CSV upload, API keys and ingest, heat map intensity slider and six palettes, landing deck, restyled sign-in, favicon, `presentation.md`.
- 151 automated checks: `tests/test_{pos,evaluate,clock,stitch,crowd,brief,google,thresholds,pooling,fusion,timing,agent,agent_page}.py` (run each with `python -m tests.<name>`; test_agent, test_evaluate and test_stitch render video and are slower). Also tested through the Flask test client and, earlier, with a headless browser for the landing and sign-in pages.
- Camera agent built and tested on synthetic video (median position error ~0.1 m; motion backend finds ~70 to 85% of true stops, under-counts stopped time). YOLO path only tested with a fake model. Not run on real footage.
- Not verified in the build sandbox: Docker build, gunicorn, Plotly rendering in a browser. (Supabase and Render deployment are set up and working per the user.)

## Known gaps and next steps
1. Run against a real Supabase project; fix anything the mocks missed.
2. Re-tune stitching, staff, party and fusion thresholds on real footage using Settings, Detection settings; all defaults were set on simulated data. Check the overlap gap between cameras on Overview before trusting merged counts.
3. Run the agent on a real clip with YOLO (`pip install -r requirements-video.txt`); tune `--conf`, `--min-track-s`.
4. Validate accuracy on a taped-off mock store and on MMPTRACK (`docs/validation-data.md`); replace proposed targets in `presentation.md` with measured values.
5. Run the new Supabase migration, then `python seed_demo.py` (safe to repeat) so the demo gets sales, start times, companions and staff. Add the Gemini key and set up Google sign-in (see above). Pooling and multi-camera fusion are built (see above) but untested on real data.
6. Landing page uses drawn illustrations, not photographs; swap in real images if wanted.
7. Make the slide deck and pitch deck from `presentation.md`.

## Working conventions
- Say what is simulated; never present demo numbers as real. Tag claims by source as in `presentation.md`.
- Keep the schema to four fields; reject extras. Service-role key stays server-side.
- UI copy: plain sentences, sentence case, no filler. Errors say what happened and how to fix it.
