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
- `app.py`: Flask factory, auth, CSRF, routes (`/`, `/login`, `/signup`, `/app`, `/app/s/<ref>/...` (incl. `/agent` setup page), `/api/s/<ref>/...`, `POST /api/ingest/v1/tracks`, `/healthz`), analysis cache, background warm-up.
- `analytics.py`: dwell, visit labels, zone stats, heat map, checkout waits (Little's law), `compare_layouts`, flags. `config.py`: thresholds.
- `stores.py` and `simulate.py`: four demo stores and the simulator. `seed_demo.py`: creates demo user and stores.
- `repo_base.py`, `repo_local.py`, `repo_supabase.py`, `repo.py`, `supa.py`: storage interface, backends, factory, REST client. `validation.py`: input checks and API key hashing.
- `templates/` (landing, agent setup, auth, store picker, overview, experiment, zones, flags, journeys, settings, zone editor), `static/` (`style.css`, `landing.css`, `app.js`, `landing.js`, favicon files).
- `agent/` (camera agent, `python -m agent`): `floor.py` homography + exclude zones, `detect.py` YOLO and motion backends,
  `tracker.py` floor-space tracker, `pipeline.py` filters and thinning, `sinks.py` CSV and ingest API (batch, retry, spool),
  `synth.py` synthetic scene + metrics, `cli.py`. `video_to_tracks.py` is a shim for the old command. `tests/test_agent.py`: 19 tests.

## Key numbers (see presentation.md for the full table)
- Stop: within 1 m for 5 s or more. Zone visit: 2 s. Heat map cells 0.5 m. Sample rate 2 Hz.
- Queue wait: Little's law; bottleneck at 4 min or more; ignore first 10 min and windows under 0.8 served per minute.
- Time-of-day baselines (`analytics.time_baselines`): 30-minute windows, a window needs 20+ shoppers and 5+ expected zone visitors; flagged when 30%+ off the zone's own average and |z| >= 3 (`config.TIME_*`). Shown on Zones (grid) and Flags ("Quieter/Busier than usual"). Needs two judgeable windows.
- Flags use 0.7x and 1.3x of the store median. Experiments use shares of shoppers with a 95% bootstrap interval; loiterers excluded; headline metric is "stopped", secondary is "reached".
- Limits: 5,000 points per ingest request; CSV 50 MB and 3 M rows; plan image 5 MB (PNG or JPEG); 40 zones per layout; passwords 8+ characters.
- Demo data: 220 to 480 visits and 45k to 119k points per layout, 0.3 to 0.8 MB compressed each, 8 recordings.

## Demo stories (simulated)
- Lakeside Grocery: offers display to the main aisle, stopped share 40% to 62%, lift +54% (+33% to +81%).
- Volt Electronics: accessories next to tills, +63% (+34% to +98%). Thread & Co.: new arrivals to the entrance, +86% (+56% to +123%).
- Corner Pharmacy: second pickup counter, median wait 3.7 to 1.3 min, peak 8.6 to 1.7; the untouched zone shows no effect.
- The simulator sets the effect sizes, so these show the method recovers a known effect, not real-world efficacy.

## Current state
- Done: time-of-day baselines per zone, agent setup page, copy buttons, all pages, auth, demo account, store CRUD, floor plan upload, zone editor, CSV upload, API keys and ingest, heat map intensity slider and six palettes, landing deck, restyled sign-in, favicon, `presentation.md`.
- Tested on the local backend through the Flask test client (54 end-to-end checks) and with a headless browser for the landing and sign-in pages.
- Camera agent built and tested on synthetic video (median position error ~0.1 m; motion backend finds ~70 to 85% of true stops, under-counts stopped time). YOLO path only tested with a fake model. Not run on real footage.
- Not verified in the build sandbox: Docker build, gunicorn, Plotly rendering in a browser. (Supabase and Render deployment are set up and working per the user.)

## Known gaps and next steps
1. Run against a real Supabase project; fix anything the mocks missed.
2. Run the agent on a real clip with YOLO (`pip install -r requirements-video.txt`); tune `--conf`, `--min-track-s`.
3. Validate accuracy on a taped-off mock store; replace proposed targets in `presentation.md` with measured values.
4. Google sign-in; POS matching; multi-camera fusion. Time-of-day baselines use minutes since the recording started, not clock time: add an optional recording start time (dataset metadata, not per point) to label real hours and to pool several days.
5. Landing page uses drawn illustrations, not photographs; swap in real images if wanted.
6. Make the slide deck and pitch deck from `presentation.md`.

## Working conventions
- Say what is simulated; never present demo numbers as real. Tag claims by source as in `presentation.md`.
- Keep the schema to four fields; reject extras. Service-role key stays server-side.
- UI copy: plain sentences, sentence case, no filler. Errors say what happened and how to fix it.
