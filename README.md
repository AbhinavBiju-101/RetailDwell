# RetailDwell: retail dwellscape intelligence

CCTV-only retail analytics. From anonymous floor positions (track number, time, x, y) it draws heat maps,
finds dead zones and queues, and tests whether a layout change worked, adjusted for how busy each day was.

Public landing page, email + password accounts, a read-only demo account with four simulated stores
(different floor plans, a different layout story each), and real accounts where stores add their own
floor plan, zones, recordings and camera-agent API keys.

## Docs

- `presentation.md`: talking points, data points and thresholds for the slide and pitch decks.
- `docs/ideation.md`: the original ideation notes.
- The product name and tagline are `BRAND_NAME` and `BRAND_TAGLINE` in `.env`.

## Run it

```bash
pip install -r requirements.txt
python app.py            # http://localhost:5000
```

With no Supabase keys it uses a local SQLite store in `data/local` and creates demo data on first start
(about a minute). The local demo login is `demo@dwellmapper.local` / `demo-demo-1`.
This mode is for development only.

## Supabase setup

1. Create a project. In **Authentication > Providers > Email**, turn **Confirm email** off for the demo
   (or leave it on; the app then asks people to confirm before signing in).
2. Run `supabase/migrations/20261010000000_init.sql` in the SQL editor (or `supabase db push`).
   It creates the tables, row level security policies and two private storage buckets (`tracks`, `plans`).
3. `cp .env.example .env` and fill in `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`,
   `SECRET_KEY`, `DEMO_EMAIL`, `DEMO_PASSWORD`.
4. `python seed_demo.py` creates the demo account and the four demo stores (safe to run again).
5. `python app.py`.

The service-role key stays on the server. Browsers never see it. Signed-in reads use the user's own token,
so row level security decides what each account sees: owners see their stores, the demo account sees only
demo stores and cannot change anything.

Google sign-in is not built yet. It is a Supabase provider setting plus one extra route.

## Docker

```bash
docker build -t dwell-mapper .
docker run --rm -p 5000:5000 --env-file .env dwell-mapper
```

## Sending camera data

Create a key in a store's **Settings > API keys** (shown once, only a hash is stored).

```bash
curl -X POST http://localhost:5000/api/ingest/v1/tracks \
  -H "Authorization: Bearer dtm_..." -H "Content-Type: application/json" \
  -d '{"layout":"A","recording":"Live","points":[{"track_id":1,"t":0.0,"x":2.1,"y":3.4}]}'
```

Exactly four fields per point, up to 5,000 points per request, x and y in metres from the bottom-left of the
floor, t in seconds. Extra fields are rejected. CSV upload in settings takes the same four columns.
The camera agent (`python -m agent`, below) produces these points from footage and sends them for you.

## Sending sales data (optional)

Sales turn the dwell data into revenue per shopper and a buy rate for every zone. A sale is tied to the shopper who finished standing at the register nearest its time; nothing about the person is used. The layout needs a zone of type `checkout`.

- Upload: Settings, Sales. CSV with exactly `txn_id, t, zone, amount` (one row per section of a basket; `t` in seconds since the recording started). Other columns are dropped.
- From the till: `POST /api/ingest/v1/sales` with a store API key and `{"layout": "A", "recording": "Live", "sales": [...]}`. Repeating a line is safe.
- The demo stores include simulated sales. Run `python seed_demo.py` again to add them to an existing demo.

## Optional settings

- **Recording start time and timezone.** Settings, Store details for the timezone; Settings, Layouts and recordings for each recording's start. With them, time-of-day findings read as clock times and a till export can use real timestamps. Supabase users run `supabase/migrations/20261011000000_start_time.sql` first. The camera agent sends the start time for live sources, or use `--started-at 2026-10-10T14:00+05:30`.
- **Staff.** Draw a zone of type `staff` (behind the counter, stockroom door). Tracks that spend time there, stand at the register for 10 minutes, or stay on the floor for 40 minutes are left out of every figure, and Overview says how many and why.
- **Store-manager brief.** The Brief page always shows a standard summary. Set `GEMINI_API_KEY` (and optionally `GEMINI_MODEL`) to let a store owner press "Write it with Gemini". Only summary figures are sent. If Gemini is unavailable, over quota, or returns something unusable, the standard summary is shown with a warning that says why. `BRIEF_DAILY_LIMIT` caps calls per store per day.
- **Sign in with Google.** Create a Google OAuth client (Web application) whose authorised redirect URI is `https://<project>.supabase.co/auth/v1/callback`, enable Google under Authentication, Providers in Supabase with its client ID and secret, add `https://<your app>/auth/callback` to Supabase's redirect URLs, then set `GOOGLE_SIGNIN=1`. The button appears on the sign-in page.

## Detection settings, several days, several cameras

- **Detection settings.** Settings, Detection settings lets you change the staff, party, camera-merging, stitching and sales-matching rules for a store. Each has a range and an explanation. Staff, party, merging and sales settings re-analyse existing recordings at once; stitching settings are fetched by the camera agent when it starts. The defaults were set on simulated data, so check them against your own footage. Supabase users run `supabase/migrations/20261012000000_settings.sql` first.
- **Several days.** Add more than one recording to a layout (Settings, Layouts and recordings) and pick "All days pooled" on any page. Give each recording a start time so hour-by-hour findings can line the days up. Weekdays and weekends are pooled as they are, so delete a recording that does not belong. Deleting removes its tracks and its sales file.
- **Several cameras.** Run one agent per camera, each with its own calibration to the same floor coordinates, the same `--layout` and `--recording`, and its own number: `python -m agent run ... --api URL --key KEY --layout A --camera 1`. Camera 0 (the default) behaves exactly as before. The server merges people seen by more than one camera and joins hand-overs between cameras. Overview shows how far apart the cameras place the same person where they overlap; if that approaches the 0.8 m limit, recalibrate before trusting the counts.

## Camera agent

Turns a video file, camera or RTSP stream into the four-field points above. Frames stay in memory for one
detection pass; nothing but `track_id, t, x, y` leaves the machine.

```bash
pip install -r requirements-video.txt                 # OpenCV, SciPy, Ultralytics (large)

# 1. Calibrate once per camera: 4 to 8 floor points you can measure with a tape
python -m agent snapshot --source rtsp://cam/stream --out frame.png     # delete the image afterwards
python -m agent click --image frame.png --width 20 --height 12 --out calib.json   # needs a display
python -m agent check --calib calib.json --image frame.png              # error in metres + a 1 m grid overlay

# 2. Run it
python -m agent run --source clip.mp4 --calib calib.json --layout A --csv tracks.csv.gz      # file out
export DWELL_API_KEY=dtm_...                                                                 # from Settings > API keys
python -m agent run --source rtsp://cam/stream --calib calib.json --layout A \
       --api https://your-app.onrender.com --recording "Live"                                # live, to the app

# 3. Score output against ground-truth floor tracks (CSV track_id,t,x,y in metres)
python -m agent evaluate --truth truth.csv --est tracks.csv --zones zones.json

# 4. Check the code itself (no camera or model needed)
python -m agent selftest        # synthetic store: prints measured position error and stops found
python -m tests.test_agent      # 19 tests, including video -> ingest API -> analysis
```

`calib.json` also takes `exclude` (floor rectangles never recorded, such as a staff room) and `floor_size`
(points outside the floor are dropped). Track ids are random per run and never joined across runs. Short
tracks (under 2 s) are held back and dropped, so flicker never reaches the store. If the app is unreachable the
points wait in `spool/` and are sent on the next start.

Detectors: `--detector yolo` (default, Ultralytics YOLO with ByteTrack) is the real one. `--detector motion`
is a no-model fallback for dry runs; it loses people who stand still for a long time and merges people who
walk together.

## Privacy by design

Only `track_id, t, x, y` exist in the schema. No video, faces, device ids or names. Reports describe groups.

## Known limits

- All demo numbers are simulated. The analysis has not seen real footage.
- The camera agent is tested on synthetic video only, and its YOLO path only against a fake model (no weights or
  network in the build sandbox). Run it on a real clip before relying on it.
- The first Plotly.js and font load needs internet access (CDN).
- Checkout wait uses Little's law, which assumes a steady state. Treat it as indicative.
