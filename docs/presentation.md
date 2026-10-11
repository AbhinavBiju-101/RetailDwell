# RetailDwell: presentation notes

Source material for the slide deck and the pitch deck. Everything here is either from the ideation doc
(`docs/ideation.md`), read from the code (thresholds, limits), or measured on the simulated demo data in this
repo. Each number is tagged so you know how far you can lean on it:

- **[doc]** a claim from the ideation doc. Typical figures from general knowledge, not measured by us.
- **[code]** a threshold or limit set in the code, so exact.
- **[sim]** measured on the simulated demo stores. Real footage has not been tested.
- **[estimate]** arithmetic from the above, with the assumption stated.
- **[target]** a proposed goal to test against. Not a result.

If a number has no tag, treat it as a claim to verify before presenting it.

The product name is set in one place (`BRAND_NAME` and `BRAND_TAGLINE` in `.env`, defaults RetailDwell and
"Retail dwellscape intelligence"). The tagline is the old working line and is easy to change.

---

## 1. One-liners

- Online stores have analytics. Physical stores have guesswork. We fix that with cameras they already own. [doc]
- We assume the hardest customer: someone who carries nothing, emits nothing, and just walks. If we can serve
  that shopper from existing cameras, every other sensor is a bonus. [doc]
- Most tools show a heat map. Few say whether a change worked. We tell a store whether moving a display
  helped, with the effect of a busier day taken out. [doc, positioning]
- Positions, not people. The video is never stored. [doc]

## 2. The problem (hackathon problem statement 3)

Physical retailers know what sold (POS) and roughly how many came in (door counter). They do not know what
happens in between: which sections get browsed, where people queue or turn back, whether a promo display is
seen at all.

Sales data shows that a section underperforms, not why. Three different causes need three different fixes:

| What happened | What it means | Fix |
|---|---|---|
| Nobody came | Route problem | Move it, or fix signage |
| They came but did not stop | Visibility problem | Raise signage, use for impulse items |
| They stopped but did not buy | Offer problem | Price, stock, confusion (needs POS to confirm) |

The brief asks for: customer journeys, real-time foot-traffic heat maps, section-by-section dwell, and
automatic flags for bottlenecks and dead zones, all from existing in-store security cameras.

Who feels it: store and operations managers, merchandisers and category managers, store planners, and
suppliers who pay for display space. Mid-size retailers most, since they cannot afford enterprise tools
but have outgrown guesswork. [doc]

## 3. What we built (and what is still a pitch)

### Working today (demo-ready)
- Heat maps on a floor plan with adjustable intensity and six colour palettes.
- Zone statistics: reach, presence dwell, engaged dwell, stop rate.
- Two dwell metrics: presence (time in a zone) and engaged (stopped in front of merchandise).
- Visit types: directed, exploratory, loiterer or waiter, pass-through, each with a confidence.
- Relative flags in plain language: dead zone, visibility problem, pass-through, destination, hot zone.
- Checkout wait estimate with Little's law, plus a bottleneck flag.
- Layout experiment engine: before/after normalised for traffic, with a 95% bootstrap interval.
- Sampled journeys with smoothing, coloured by visit type.
- Accounts (email and password via Supabase Auth), a read-only demo account, four simulated stores.
- Floor plan upload, rectangle zone editor, multiple layouts per store, CSV upload.
- Ingest API for camera agents with per-store API keys.

### Not built yet (say so, do not imply otherwise)
- Camera agent validation on real footage. The agent (`python -m agent`) exists and is tested on synthetic video
  only [sim-video: median position error 0.09 to 0.11 m, 90th percentile 0.22 to 0.25 m over three seeds, with the
  no-model motion detector; YOLO path not yet run on real footage].
- Accuracy validation on real footage.
- Multi-camera fusion, POS matching, revenue per visitor, what-if simulation, time-of-day baselines.
- Google sign-in (planned).

### Tech stack (as built)
Python, Flask, Jinja, Plotly.js (CDN), Supabase (Auth, Postgres with row level security, Storage),
pandas and NumPy for analysis. The ideation doc proposed Streamlit; we moved to Flask for control over layout.
Planned for the agent: Ultralytics YOLO (nano or small), ByteTrack or BoT-SORT, OpenCV homography. [doc]

## 4. The anonymous walker

Everything useful must be derived from four observable things [doc]:
1. Where their body is on the floor, over time
2. How they move: speed, direction, stopping, turning back
3. Who they move with (co-movement)
4. Where and when they appear at the entrance and the checkout

Choices that follow [doc]:

| Decision | Choice |
|---|---|
| Primary sensor | CCTV only. The full pipeline works with nothing else |
| Identity | Short-lived tracks, deleted after the visit |
| Dwell | By displacement: within about 1 m for 5+ seconds, not by carrying anything |
| Parties | Inferred from co-movement from the entrance |
| Phones, PIR, BLE, tags | Optional confidence boosters or validation, never required |
| Conversion | Checkout-time matching, not devices |

## 5. Privacy and data volume

### Stance
- No face recognition. No stored video. Anonymous coordinates only. [doc]
- Track IDs are short-lived and never matched across days. No repeat-visitor identity, by design. [doc]
- Phone identifiers are avoided: collecting them needs consent under India's DPDP Act. [doc]
- Loiterer and staff labels are aggregate analytics only. A suspicious-person alert would turn the tool into
  surveillance. [doc]
- Edge processing is a pitch line that supports the main features, not a feature of its own. [doc]

### Privacy enforced in the product [code]
- The schema holds four fields: `track_id`, `t`, `x`, `y`. Nothing else has a column.
- CSV upload keeps only those four columns and drops the rest. The ingest endpoint rejects any point with
  extra fields instead of storing it.
- API keys are shown once and stored as a SHA-256 hash. A key can only add points to its own store.
- Row level security in Postgres: owners see only their stores; the demo account sees only demo stores
  and cannot change anything. The service-role key stays on the server.

### Volume of data
- Video kept: **none**. [doc]
- The doc's figure for the video this replaces: about **300 GB per week** of footage becoming **a few MB**. [doc]
- Stored sample rate: **2 points per second per person** [code, `SAMPLE_HZ`].
- Demo recordings: **44,779 to 119,075 points per layout**, **220 to 480 visits per layout**, about
  **2 hours** each, **0.3 to 0.8 MB compressed** per recording. Roughly **200 points per visit** and
  **about 7 bytes per point compressed**. [sim]
- Total demo data: 8 recordings, about 5 MB compressed in all. [sim]
- Worked estimate for a real store with **1,000 visits a day** at the demo's ~200 points per visit:
  about 200,000 points a day, **about 1.5 MB a day compressed, about 10 MB a week, about 500 MB a year**.
  Against roughly 300 GB of video a week, that is more than **four orders of magnitude smaller**. [estimate]
  Longer visits scale it linearly.
- Ingest limits: **5,000 points per request**; at 2 Hz that is 41 minutes of one person, or about 2 minutes
  of 20 people at once. [code, estimate]
- Upload limits: **50 MB and 3 million rows** per CSV. [code]

## 6. Why cameras: the sensor comparison

Only CCTV, PIR, mmWave and overhead stereo can see the anonymous walker. Of those, only CCTV matches the brief
and costs nothing in hardware. Anything carrying identity (phones, tokens) adds sampling bias; anything with
precise position (stereo, tokens) needs new hardware. [doc]

| Approach | Sees phone-less, token-less walkers | Typical position accuracy | Bias | Privacy and legal risk | Setup cost | Fits the brief |
|---|---|---|---|---|---|---|
| Existing CCTV and CV | Yes | about 0.5 to 1 m near the centre, worse at distance or occluded | Low | Low if no video stored | Low (hardware exists) | **Yes** |
| Overhead stereo cameras | Yes | about 0.2 to 0.5 m | Low | Low | High (new hardware) | No |
| Passive Wi-Fi or BLE sniffing | Phone carriers only | 2 to 5 m, worse near metal | Phone carriers | High | Low to moderate | No |
| In-app BLE beacons | App users only | 1 to 3 m with dense beacons | App installers | High (consent, DPDP) | App development | No |
| Mobile GPS | App users only | Poor indoors (tens of metres) | Heavy | High | App development | No |
| Passive RFID tokens | Token holders only | Zone level at readers | Compliance and loss | Moderate | Moderate | No |
| Offline IMU logger tokens | Token holders only | Drifts, needs landmark correction | Compliance and loss | Moderate | Moderate to high | No |
| Shelf PIR or ToF | Yes, anonymously | Per-shelf presence | None | Very low | Low per unit | Add-on |
| mmWave radar | Yes, anonymously | Presence and counting by area | None | Very low | Moderate | Add-on |
| POS checkout match | Only at checkout | A timestamp anchor | Purchasers only | Low | Low (data feed) | Data source |

All accuracy figures are typical ranges from general knowledge, not measurements. Validate on your own
footage before quoting them. [doc]

Why phones are dropped as an identity source: GPS fails indoors; Wi-Fi and BLE addresses are randomised on
modern phones; people carry several devices, or none, or share one; metal shelving blocks Bluetooth; collecting
identifiers needs consent; the only reliable route is an app most walk-in shoppers will not install. [doc]

Tokens are a validation and roadmap item, not the product. Where they help: ground truth for the accuracy slide
(give tokens to 10 to 15 friends and compare paths with CCTV tracks), checkout linking for token holders, and a
premium tier. [doc]

Cheapest useful hardware for a hackathon: a few shelf PIR sensors on an ESP32. No PIR triggers plus no camera dwell
is a very confident dead zone. [doc]

**Positioning against commercial products** (verify against their sites before quoting): overhead-sensor vendors
get accuracy from purpose-built calibrated hardware and a CAD or PDF floor plan supplied by the retailer. Our gap
is comparable insight from cameras already mounted for security, with self-serve calibration for stores with no
clean floor plan. Our edge is affordability, privacy, relative flagging and actionable output. [doc]

## 7. Pipeline

```
Camera / RTSP stream (or video file for the demo)
  -> frame grab, adaptive sampling, presence gate
  -> YOLO detection + ByteTrack / BoT-SORT tracking
  -> homography: pixels -> floor-plan coordinates
  -> zone state machine (enter / exit, dwell, displacement)
  -> batched writes every few seconds  (in the app: POST /api/ingest/v1/tracks)
  -> aggregates: heat map, occupancy, zone stats, queue waits
  -> dashboard, flags, layout experiments
```

| Stage | Parameter | Value |
|---|---|---|
| Sampling | Frames per second | 2 to 5, adaptive: about 1 when empty, more when busy [doc] |
| Sampling | Image size and model | Downscale to about 640 px, small YOLO model [doc] |
| Sampling | Frame skipping | By person presence, never by pixel change (a still shopper carries the dwell) [doc] |
| Tracking | Tracker frame rate | About 8 to 10 fps, or report the ID-switch rate [doc]. At 2 fps a walker moves about 0.6 m per frame [doc] |
| Stitching | Gap rule | Same area, similar velocity, gap under a few seconds [doc] |
| Floor mapping | Method | Homography from 4+ measured floor points, with undistortion for wide-angle lenses [doc] |
| Floor mapping | Accuracy statement | Zone level, not centimetre level [doc] |
| Storage | Per detection | `track_id, t, x, y` |

Getting the store map without a blueprint (differentiator): (1) upload a photo or sketch and scale it with one
known distance, (2) define zones in image space and skip the metric plan, or (3) tape-measure four or more floor
points visible in the camera. [doc]

## 8. Analytics model and thresholds

### Definitions and thresholds as implemented [code]

| Item | Value |
|---|---|
| Stop (dwell) | Stay within **1.0 m** for at least **5.0 s** (`DWELL_RADIUS_M`, `DWELL_MIN_S`) |
| Visit to a zone | At least **2.0 s** inside it (`MIN_PRESENCE_S`) |
| Heat map resolution | **0.5 m** cells, weighted by time |
| Checkout queue area | The checkout zone widened **3.0 m** to the left and **0.5 m** up (`QUEUE_MARGIN_M`) |
| Queue window | **300 s** by default, set per store (the demo pharmacy uses 600 s) |
| Wait estimate | Little's law: people in the queue area divided by people served per minute |
| Wait validity | Ignore the first **10 min** (warm-up) and windows serving under **0.8 per minute** |
| Bottleneck flag | Estimated wait of **4 min** or more |
| Dead zone | Visits under **0.7x** the store median and dwell under **0.7x** |
| Visibility problem | Visits under **0.7x** the median, dwell not low |
| Pass-through | Visits over **1.3x** the median, dwell under **0.7x** |
| Hot zone | Visits over **1.3x** and dwell over **1.3x** |
| Destination | Visits not low, dwell over **1.3x** |
| Directed visit | **2 or fewer stops** and less than **30 s** engaged time |
| Other visit types | Exploratory (more stops), loiterer or waiter (long stop near entrance, seating or empty floor), pass-through |
| Experiment metric | Share of shoppers who stopped at a zone (headline) and who reached it (secondary) |
| Experiment interval | 95% bootstrap, **1,500 to 2,000** resamples, on relative lift in share |
| Experiment population | Shoppers only. Loiterers and waiters are excluded |
| Zone typing | merchandise, checkout, entrance, seating. A long stop in a non-merchandise zone counts as presence, not engagement |

Flag thresholds use the store-wide median as a stand-in. The fallback chain from the doc: category preset, then
store-wide percentile, then the zone's own time-of-day baseline once history exists. Only the middle step is built. [doc]

### Dwell versus visits [doc]

| | Low dwell | High dwell |
|---|---|---|
| High visits | Pass-through: good for impulse items and promos | Hot, engaged zone: protect it |
| Low visits | Dead zone: relocate, redesign or fix signage | Destination: needs seating, demos, staff |

### Attention, outcome and mission [doc]

Dwell is attention. Purchase is outcome. A hidden third variable is the shopper's mission. A list-driven shopper has
short dwell and high purchase; an explorer has long dwell and uncertain purchase. Averaging hides both. Mission is
inferable from camera data: directed means direct paths, few stops, short visit; exploratory means meandering,
many stop-and-go episodes, backtracking, longer visit. Compute dwell against purchase per segment.

### Bottlenecks [doc]
A queue is not a crowd. High occupancy plus low speed also fires on happy browsers, so for chokepoints measure
throughput (people crossing a line per minute) alongside occupancy. Little's law needs steady state, so treat it as
indicative during surges.

### Loiterers and wanderers [doc]
Zone typing, two dwell metrics, optional pose-based interaction, soft visit-level labels with confidence, and
time-of-day correlation (an afternoon loiterer spike is itself the free-AC finding). Wanderers who buy a drink
or chocolate are revenue, so they stay a segment rather than noise. Thresholds are configurable by store type.

## 9. Layout experiment engine (the spine)

Before/after comparison must be normalised for total traffic, otherwise a busy Saturday looks like a successful
move. We compare the share of shoppers who stopped at a zone before and after, with a bootstrap interval. [doc, code]

Why this is the lead: existing tools show heat maps, few say whether a change worked; it is very demoable because
you control both layouts and know the truth; and it has a business model, because suppliers who pay for display
space want proof of exposure. [doc]

Caveats to state: association, not proof; the cleanest test compares days of the same type with the same
promotions; stopping is the headline metric because a display moved onto a main path is walked past more, which
would flatter a plain reach count. [code, design note]

### The four demo stories [sim]

All four are simulated. The size of each effect was set when the simulator was written, so these results show that
the pipeline recovers a known effect and ignores a non-effect. They are not evidence about real shops.

| Store (floor) | Change | Result |
|---|---|---|
| Lakeside Grocery (20 x 12 m) | Weekly offers display moved from the far wall to the main aisle | Stopped share **40% to 62%**, lift **+54%** (95% range +33% to +81%). Raw stops rose **+111%** but 480 shoppers came on day B against 350, which is the part normalisation removes |
| Volt Electronics (26 x 14 m) | Accessories rack moved next to the tills | Stopped share **33% to 53%**, lift **+63%** (+34% to +98%). Reach rose +176%, which overstates it, hence stop is the headline |
| Thread & Co. (24 x 16 m) | New-arrivals table moved to the entrance | Stopped share **34% to 63%**, lift **+86%** (+56% to +123%) |
| Corner Pharmacy (14 x 10 m) | A second pickup counter opened, same floor plan | Median estimated wait **3.7 min to 1.3 min**; worst window **8.6 min to 1.7 min**. The untouched Everyday health zone showed **-6%** with a range of -23% to +12%, so no effect, as it should |

Simulator settings behind them [code, sim]: 2 hour recordings, 220 to 480 visits per layout, fixed seeds so the
numbers are reproducible, walking speed 1.4 m/s for directed shoppers and 0.8 m/s for exploratory ones, roughly 8% loiterers
and waiters in most stores (15% to 20% in the pharmacy, which has seating).

Suggested demo order: pharmacy (a queue, an alert that fires early), then grocery (the experiment with the
normalisation callout), then one of electronics or apparel.

## 10. Accuracy and validation plan

Most hackathon CV demos show overlays and no accuracy numbers. One accuracy slide separates this project. [doc]

### Plan [doc]
1. Annotate 2 to 3 minutes of clip by hand: when each person enters and leaves each zone.
2. Report dwell error, count error and ID switches.
3. Record a taped-off mock store for ground truth and a controlled camera angle.
4. Script friends to act out waiting, browsing and directed shopping, so mission and loiterer labels have ground truth.
5. Optional: tokens on 10 to 15 friends, or shelf PIR sensors, as independent ground truth.
6. State accuracy honestly as zone level, not centimetre level.

### Metrics to report [doc]
Dwell error per zone (seconds and percent); count error at entrance and checkout; ID switches per minute at the
chosen fps; journey completeness (share of visits stitched entrance to checkout); mission and loiterer
classification accuracy on scripted clips.

### Proposed targets [target]
These are suggestions to agree on before testing, then replace with measured values. None has been measured.

| Metric | Proposed target |
|---|---|
| Zone assignment of a stop | At least 90% of annotated stops land in the correct zone |
| Dwell error per zone | Median within 20% of the annotated value |
| Entrance and checkout count error | Within 10% |
| ID switches | Reported per minute at the chosen fps; aim to keep low enough that dwell error stays within target |
| Journey completeness | Report the share; aim for a clear majority of entrance-to-checkout visits |
| Mission and loiterer labels | Report accuracy on scripted clips; do not quote a number before measuring |
| Position error | Report as a distance distribution; claim zone level only |

Known tracking risks and handling [doc]:

| Risk | What goes wrong | Handling |
|---|---|---|
| Low fps | IoU association shaky, IDs fragment | Tracker near 8 to 10 fps, or report ID-switch rate |
| Occlusion | Track breaks and restarts, inflating counts and dwell | Short-gap stitching |
| Staff | Fake dwell at counters and shelves | Staff-only zones, flag implausibly long or looping tracks |
| Children and groups | One family is several tracks | State the counting unit: individuals for dwell, parties for conversion |
| Fragmented journeys | Entrance-to-exit paths break | Zone-sequence flows with stitching, labelled probable with a confidence figure |

## 11. Product branches and decision

| Branch | Intent | Demo moment | Main risk |
|---|---|---|---|
| 1 Store manager's diagnostician | Say why a section underperforms and what to try | "Aisle 4 end-cap: visits 35% below usual, dwell normal, so people are not seeing it. Move signage." | Generic without concrete rules; needs baseline history |
| 2 Layout experiment engine | Turn "we moved the display" into measured evidence | Two clips of one mock store: +22% visits to the promo zone after normalising (doc's example) | Needs a consistent camera and two comparable recordings |
| 3 Live operations console | Real-time awareness: "checkout wait about 6 min, open another counter" | A queue builds and an alert fires early | Closest to existing vendors |
| 4 Conversion funnel | Split each zone into passed, stopped, bought | Two zones with equal sales and different funnels | POS data is mocked |
| 5 Privacy-first edge kit | Deployable by a small retailer in under an hour | Calibrate a new camera live in minutes | Weak if the analytics are shallow |

Decision: **Branch 2 as the spine, Branch 1 as support** ("measure the change, then explain it"). The queue wait
estimate borrows the demo moment of Branch 3. [doc]

## 12. Roadmap, MVP tiers and limits

MVP cut [doc]:
- Must have: detect and track, homography, zones with dwell, heat map, one bottleneck rule (checkout throughput),
  one relative dead-zone rule with a plain-language flag.
- If time allows: before/after comparison, staff filtering, mission segmentation, two dwell metrics, loiterer segment.
- Pitch only: multi-camera fusion, POS beyond a mock, what-if simulation, tokens and BLE, appearance re-identification.

Identity tiers [doc]: Tier 0 per-camera tracking, floor coordinates, zone dwell, heat map; Tier 1 dedupe across
overlapping cameras by spatial gating (within about 0.5 to 1 m at the same moment, Hungarian assignment); Tier 2
party clustering by co-movement (within about 2 m with similar velocity, seeded at the entrance); Tier 3 BLE anchors,
POS time matching, appearance re-ID. A single camera with Tiers 0 to 2 and a measured accuracy slide beats a
five-source fusion that was never validated.

Roadmap [doc]: multi-camera fusion; POS and loyalty integration (revenue per visitor by matching the party at the
register to a POS timestamp and lane); layout what-if simulation; time-of-day comparisons; engagement versus
passing-by using head orientation; optional anchors (tags, PIR, mmWave).

Honest limits to state [doc, code]:
- Occlusion and ID switches happen when people cross paths. We report them.
- Single camera for the prototype. Multi-camera stitching is future work.
- CCTV shows behaviour, not purchases. Conversion needs POS data.
- Path to purchase is correlation, not causation.
- Journeys are probable, with a confidence figure.
- Commercial products exist.
- The current demo runs on simulated shoppers.
- The camera agent and the Docker image have not been tested end to end.

## 13. Suggested deck outlines

### Pitch deck (about 8 to 10 slides, 5 to 7 minutes)
1. Hook: online stores have analytics, physical stores have guesswork.
2. The gap: sold, came in, and nothing in between. The three-causes table.
3. The hardest customer: carries nothing, identifies nothing.
4. Why cameras: the sensor comparison, one row highlighted.
5. Privacy: four numbers per detection, no video; the data volume comparison.
6. Live demo: pharmacy queue, then grocery experiment with the normalisation callout.
7. Why it is different: experiment engine plus plain-language flags.
8. Accuracy: what we report, zone-level honesty, the validation plan.
9. Business: mid-size retailers, and suppliers who pay for display space want exposure proof.
10. Roadmap and ask.

### Technical slide deck (adds)
Pipeline diagram and risks; homography and the no-blueprint calibration options; the analytics thresholds table;
the experiment method (shares, bootstrap, why stop is the headline); data model, RLS and the ingest API;
identity tiers; limitations.

### Interactive tour on the landing page
The landing page already has 13 scrolling slides covering the problem, the walker, sensor comparison, privacy,
pipeline, dwell quadrants, shopper missions, a queue-wait calculator, a before/after normalisation calculator,
flags, accuracy and roadmap. It can double as the first draft of the deck.

## 14. Likely questions from judges

- **Does it work on real footage?** The analytics and the app are built and tested on simulated data with a known
  effect. The camera agent and the validation on real footage are the next milestone, and we state accuracy at zone level.
- **How do you know a layout change caused the lift?** We do not claim causation. We normalise for traffic and
  give a likely range, and the cleanest test compares similar days with the same promotions.
- **What about privacy?** No video stored, no faces, four fields per detection, short-lived track IDs,
  aggregates only, no per-person alerts. Schema, upload and ingest enforce it.
- **How is this different from existing vendors?** They rely on purpose-built overhead sensors and a supplied
  floor plan. We use existing cameras, self-serve calibration, relative flags that explain themselves, and
  before/after experiments that account for traffic.
- **What about staff and children?** Staff-only zones and flags for implausibly long tracks; dwell counts
  individuals, conversion would count parties.
- **Why not phones or an app?** Address randomisation, indoor GPS failure, selection bias, and consent needs under
  the DPDP Act.
- **Business model?** Subscription for retailers; suppliers who pay for display space want proof of exposure.
  [doc, idea only, not validated]
- **What does it cost to run?** Coordinates are tiny (about 500 MB a year for a 1,000-visit-a-day store by our
  estimate), so storage is a small part of the bill. The camera agent's compute is the real cost and is untested.

## 15. Glossary

- **Dwell:** staying within about 1 m for 5 seconds or more.
- **Presence dwell:** total time inside a zone. **Engaged dwell:** time stopped in front of merchandise.
- **Reach:** share of shoppers who spent at least 2 s inside a zone. **Stop rate:** share who stopped there.
- **Mission:** the shopper's purpose, inferred from movement (directed or exploratory).
- **Homography:** the mapping from camera pixels to floor-plan coordinates.
- **Little's law:** wait = number waiting divided by service rate.
- **Lift:** relative change in the share of shoppers, before to after.
- **Track:** an anonymous short-lived fragment of one person's path from one camera.

## 16. Open items to confirm before presenting

- Hackathon length and team size, candidate footage, camera angle and lens, whether a checkout is in frame. [doc, still open]
- Whether a mock store can be recorded for ground truth.
- Whether anyone can build the ESP32 PIR or token validation.
- Replace every [target] and [doc] accuracy figure with a measured one when you have footage.
- Confirm competitor claims against their own sites.
