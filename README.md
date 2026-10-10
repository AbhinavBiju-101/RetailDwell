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
`video_to_tracks.py` is a starting point for an agent that produces these points from footage.

## Privacy by design

Only `track_id, t, x, y` exist in the schema. No video, faces, device ids or names. Reports describe groups.

## Known limits

- All demo numbers are simulated. The analysis has not seen real footage.
- `video_to_tracks.py`, the Docker image and the Supabase backend were written without access to a
  running Docker daemon or a live Supabase project; verify them against your own project first.
- The first Plotly.js and font load needs internet access (CDN).
- Checkout wait uses Little's law, which assumes a steady state. Treat it as indicative.
