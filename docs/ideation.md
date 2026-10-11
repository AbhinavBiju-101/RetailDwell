# Overview

Working notes for the hackathon problem **Dwell-Time & Bottleneck Mapper**. Each tab holds one thread of the discussion. Add sub-tabs as ideas branch, and mark what is decided versus still open.

> Online stores have analytics. Physical stores have guesswork. We fix that using cameras they already own.

### Where things stand

* **Base sensor:** existing CCTV, camera-first and sensor-agnostic.  
* **Design constraint:** the system must give useful output for a shopper who carries nothing and identifies nothing.  
* **Analytical core:** attention (dwell) and outcome (purchase) are separate variables, moderated by shopper mission.  
* **Leading product direction:** layout experiment engine as the spine, with the diagnostician as support (see Product branches).  
* **Blocking unknowns:** hackathon length, team size, footage, camera angle, whether a checkout is in frame (see Open questions).

# **The brief**

Problem statement 3\.

> Physical retail stores struggle to measure which store sections attract long browsing times versus areas that block traffic or are ignored completely. Optimizing a store's layout, promotional display placement, and traffic flow directly impacts revenue and customer satisfaction. By utilizing existing, standard in-store security cameras, computer vision can unlock deep operational insights, transforming raw video feeds into actionable heat maps and flow metrics.

#### **The ask**

Design and build an intelligent computer vision solution that processes standard in-store camera feeds to:

* map customer journeys  
* generate real-time foot-traffic heat maps  
* calculate precise section-by-section dwell times  
* automatically flag layout bottlenecks or dead zones

The brief names existing cameras explicitly, so anything that needs new hardware reads as optional.

# **Problem and who feels it**

Physical retailers know **what sold** (POS) and roughly **how many came in** (door counter). They do not know what happens in between:

* Which sections do shoppers browse, and which do they walk past?  
* Where do people get stuck, queue, or turn back?  
* Is that promo display being seen at all?

Sales data shows *that* a section underperforms, not *why*. Nobody came? They came but did not stop? They stopped but did not buy? Each needs a different fix.

#### **Who feels it**

Store and operations managers, merchandisers and category managers, store planners, and suppliers who pay for display space. Mid-size retailers feel it most: they cannot afford enterprise tools but have outgrown guesswork.

# **Design principles**

Decisions that should survive every later change of mind.

* The system must produce useful output for a shopper who carries nothing and identifies nothing.  
* CCTV is the base. Every other sensor is an optional layer that raises confidence or validates accuracy.  
* Report each metric at the unit it needs: individual, party or sample.  
* Show confidence on anything inferred (journeys, parties, loiterer labels). Say probable, never certain.  
* Aggregates only. Nothing that singles out a person.

# **The anonymous walker**

The target customer: no phone signal, no cart, no app, alone or in a party, free to wander, with a big wallet and no interest in being identified.

#### **What can be observed about this person**

* Where their body is on the floor, over time  
* How they move: speed, direction, stopping, turning back  
* Who they move with (co-movement)  
* Where and when they appear at the entrance and checkout

Everything useful must be derived from those four things.

#### **Consequences**

| Decision | Choice |
| ----- | ----- |
| Primary sensor | CCTV only. The full pipeline works with nothing else. |
| Identity | Short-lived tracks, stitched within a visit, deleted after. |
| Cross-camera matching | Floor position and timing first. Clothing appearance only as a tiebreaker, held in memory. |
| Parties | Inferred from co-movement from the entrance. |
| Dwell | Defined by displacement (stays within about 1 m for 5+ seconds), not by carrying anything. |
| PIR, BLE, tags | Optional confidence boosters or validation, never required. |
| Conversion | Checkout-time matching, not devices. |

#### **Pitch framing**

> We assume the hardest customer: someone who carries nothing, emits nothing, and just walks. If we can serve that shopper from cameras the store already owns, every other sensor is a bonus.

# **Privacy and video handling**

#### **Never store the video**

Video is a means, not the product. Process the live stream (RTSP) frame by frame and keep only `(track_id, timestamp, x, y)`. About 300 GB per week becomes a few MB.

#### **Stance**

* No face recognition. No stored video. Anonymous coordinates only.  
* Track IDs are short-lived and deleted after the visit. No repeat-visitor identity across days, by design.  
* Collecting phone identifiers needs consent under India's DPDP Act, which is another reason to drop phones as an identity source.  
* Edge processing is a pitch line that supports the main features, not a feature of its own.  
* Loiterer or staff labels are aggregate analytics only. A suspicious person alert would turn the tool into surveillance.

# **Pipeline**

How raw camera frames become floor coordinates, zones and metrics. Sub-tabs cover each stage and where it is fragile.

# **Architecture**

Camera / RTSP stream (or video file for the demo)  
  \-\> frame grab, adaptive sampling, presence gate  
  \-\> YOLO detection \+ ByteTrack/BoT-SORT tracking  
  \-\> homography: pixels \-\> floor-plan coordinates  
  \-\> zone state machine (enter/exit, dwell, displacement)  
  \-\> batched writes to SQLite/Postgres every few seconds  
  \-\> rolling aggregates (decaying heat map, occupancy, 5-min zone stats)  
  \-\> dashboard \+ alert/recommendation engine

Raw coordinates are kept (they are tiny) for week-over-week and before/after layout analysis.

#### **Sensor-agnostic fusion layer**

Make every sensor emit the same simple event: `(zone, time, count, confidence)`. CCTV, PIR, cart tags and checkout timestamps all become inputs to one fusion layer. Adding a sensor then means writing an adapter. The pitch becomes camera-first and sensor-agnostic instead of five separate integrations.

#### **Proposed stack**

Python, Ultralytics YOLO (nano or small), ByteTrack or BoT-SORT, OpenCV (`grab`/`retrieve`, `findHomography`), NumPy, SQLite or Postgres, Streamlit (or React), Plotly.

# **Sampling, fps and dwell**

#### **Sampling plan from the README**

* Sample at 2-5 fps, adaptive: about 1 fps when empty, higher when busy.  
* Downscale to about 640 px and use a small YOLO model.  
* Always process the latest frame and drop stale ones if the pipeline falls behind.

#### **Do not skip frames by pixel change**

A shopper standing still at a display barely changes pixels, and those are exactly the frames that carry dwell. Gate on person presence instead: a cheap detector at low rate with a hold time after the last person leaves. Or use background subtraction with a minimum blob size to ignore fans and screens.

#### **Is low fps a problem?**

* Dwell is measured in seconds, so it is robust.  
* Per-frame velocity is fragile. Compute speed over a 3-5 second window.  
* Define browsing by displacement: stays within a 1 m radius for 5+ seconds.

> **Open risk:** 2-5 fps can break tracking. ByteTrack and BoT-SORT assume small motion between frames, and at 2 fps a walker moves about 0.6 m per frame. Either run the tracker at about 8-10 fps (the detector can stay cheap), or measure and report the ID-switch rate at the chosen fps.

# **Homography and the floor map**

The weakest link in the geometry. A single homography assumes a flat floor and an undistorted image. Real CCTV is often wide-angle or fisheye, mounted at an angle, with shelves hiding feet. The bottom-centre of a bounding box is therefore a noisy floor point, and error grows with distance from the camera.

#### **Plan**

* Add lens undistortion, or at least a camera-model check.  
* Report accuracy as zone-level, not centimetre-level. That is honest and enough for dwell by zone.

#### **Getting the store map without a blueprint**

Enterprise vendors usually get a CAD or PDF floor plan from the retailer and calibrate sensors against it. A mid-size store often has no clean plan, which makes self-serve calibration a real differentiator. Options:

1. **Draw it yourself:** upload a photo or sketch, scale it with one known distance (door width, floor tile), and draw zones on it.  
2. **Use the camera view as the map:** define zones in image space and skip the metric plan. You lose cross-camera stitching but not dwell or heat maps.  
3. **Measure and click:** tape-measure 4+ floor points visible in the camera and compute the homography. This is what the README already proposes.

# **Tracking risks**

| Risk | What goes wrong | Handling |
| ----- | ----- | ----- |
| Low fps | IoU association gets shaky, IDs fragment when people cross | Run tracker at about 8-10 fps, or report ID-switch rate |
| Occlusion | A track breaks and restarts, creating a second person and inflating counts and dwell | Short-gap stitching: same area, similar velocity, gap under a few seconds |
| Staff | Stocking shelves or standing at counters creates huge fake dwell | Staff-only zones, flag implausibly long or looping tracks, optional coarse uniform cue |
| Children and groups | A family of four is one shopping unit but four tracks | State the counting unit: individuals for dwell, parties for conversion |
| Full journeys | Track fragmentation breaks entrance-to-exit paths | Zone-sequence flows with stitching, labelled probable journeys with a confidence figure |

# **Analytics model**

What we compute from tracks, and how we turn it into flags and recommendations.

# **Visits by dwell**

|  | Low dwell | High dwell |
| ----- | ----- | ----- |
| **High visits** | Pass-through zone: good for impulse items and promos | Hot, engaged zone: protect it |
| **Low visits** | Dead zone: relocate, redesign, or fix signage | Destination zone (for example TVs): needs seating, demos, staff |

Add conversion (dwell plus POS) to tell engagement from confusion, and speed plus crowd density to tell engagement from congestion.

# **Attention, outcome and mission**

Dwell measures **attention**. Purchase measures **outcome**. They are independent, and there is a hidden third variable: the shopper's **mission**.

|  | Low dwell | High dwell |
| ----- | ----- | ----- |
| **High purchase** | Directed shopper with a list: knew what they wanted, grabbed it | Engaged and converted: the healthy case |
| **Low purchase** | Walked past: visibility problem | Interested but did not buy: price, stock, confusion or comparison shopping |

A list-driven shopper has short dwell and high purchase. An explorer has long dwell and uncertain purchase. Averaging them hides both patterns.

#### **Mission is inferable from camera data alone**

* **Directed:** direct paths, high walking speed between zones, few stops, short visit.  
* **Exploratory:** meandering paths, many stop-and-go episodes, backtracking, longer visit.

Compute the dwell-versus-purchase matrix **per mission segment**, not store-wide. Example finding: zone C converts well for directed shoppers, but explorers dwell and leave empty-handed.

#### **Where purchase per zone comes from**

POS baskets contain SKUs, and each SKU maps to a shelf zone. At checkout, match the party to a basket and ask, for each zone the party visited, whether the basket holds an item from it.

* Visited the zone and bought from it: converted.  
* Visited and did not buy from it: browsed.  
* Did not visit but still bought from it: a tracking miss, or the item sits elsewhere. A useful data-quality check.

Caveats: the SKU-to-zone map needs upkeep (items sit in several places), and attribution is correlation only. The join happens at party level.

# **Bottlenecks and throughput**

The README rule, high occupancy plus low speed, also fires on happy browsers in a popular aisle. Make it stricter.

* For chokepoints (checkout, entrance, narrow aisles) measure **throughput**: people crossing a virtual line per minute, alongside occupancy.  
* Estimate wait time with Little's law: occupancy divided by throughput. Example output: checkout queue, about 6 minutes estimated wait.  
* This separates a queue from a crowd and gives a metric a manager can act on.  
* Keep dwell and congestion apart using speed, displacement and crowd density.

Raw Little's-law estimates need steady-state conditions, so treat them as indicative during surges.

# **Relative flagging**

The strongest differentiator, and it has a cold-start problem: a zone's own norm needs history.

#### **Fallback chain**

1. Category profile preset  
2. Store-wide percentile  
3. The zone's own time-of-day baseline once data accumulates

#### **Every flag explains itself**

> Aisle 4 end-cap: visits 35% below usual Friday evening, dwell normal. People are not seeing it. Move the signage to sightline height.

For the demo, seed or simulate baseline history so the flags have something to compare against.

# **Wanderers and loiterers**

Some people enter for the free AC, to wait for a friend, or to browse casually. You cannot read intent, but you can see behaviour. The goal is to stop non-shoppers inflating dwell and to report them as their own segment.

| Signal | Browsing shopper | Loiterer or waiter |
| ----- | ----- | ----- |
| Location of the stop | In front of merchandise | Near the entrance, seating, vents, windows or empty floor |
| Interaction | Reaches, picks up, turns toward shelves | Hands down, body faces the door or a phone |
| Movement | Stop-and-go pattern | One long stop, or slow pacing in a small area |
| Zone coverage | Several zones | One or two zones for the whole visit |
| Exit | Often via checkout | Leaves without purchase, or leaves with someone who just arrived |

#### **Approaches, cheapest first**

1. **Zone typing:** tag zones as merchandise, circulation, seating or entrance. A 10-minute stop in circulation counts as presence, not engagement.  
2. **Two dwell metrics:** presence dwell (time in zone) and engaged dwell (time stopped in a merchandise zone with an interaction signal).  
3. **Pose-based interaction:** arm extension toward a shelf and body orientation from a pose model. Test on your footage first, since accuracy drops at low resolution and steep angles.  
4. **Visit-level segmentation:** soft, confidence-scored labels: engaged shopper, pass-through, loiterer or waiter, possible staff.  
5. **Time-of-day correlation:** a loiterer spike in the afternoon heat is itself the free-AC finding.

#### **Why it is an asset**

* Wanderers who buy juice or chocolate are still revenue. Keep them as a segment, not noise. Impulse purchases near checkout are a finding.  
* Conversion among engaged visitors is more honest than conversion over all visitors.  
* A crowd lingering at the entrance may be blocking flow to the first display, which is a bottleneck by our own definition.

#### **Boundaries**

* Labels carry confidence scores. Someone resting, comparing slowly, or moving carefully can look like a loiterer.  
* Aggregates only. Never an alert about an individual.  
* Thresholds are configurable, since a furniture store differs from a grocery store.

# **Identity and fusion**

Fuse at different levels, and only as deep as each metric needs. Do not try to build one unified identity.

| Metric | Needs identity? |
| ----- | ----- |
| Heat map, zone occupancy, throughput | No, only detections at floor positions |
| Per-zone dwell | Only a short, reliable track per person |
| Zone-to-zone flow | Needs stitching across gaps |
| Conversion (dwell plus purchase) | Needs a party linked to a basket |

# **Entity layers**

1. **Track:** a short fragment from one camera. The raw output.  
2. **Person:** tracks stitched across cameras and gaps.  
3. **Party:** people who shop together (a family, a couple).  
4. **Visit:** a party's full trip from entrance to checkout.

Each level carries a confidence score. Report each analytic at the level it needs.

| Layer | Unit | Source |
| ----- | ----- | ----- |
| Heat map, zone dwell, occupancy | Individual person | CCTV, which sees everyone including children and token-less members |
| Purchase attribution, conversion | Party | POS basket joined at checkout |
| Validation and calibration | Whoever carries a token | Tokens, as a sample only |

# **Multi-camera deduplication**

Position first, appearance second.

#### **Overlapping cameras**

Project every detection onto the shared floor plan with time-synced frames. If two cameras report a person within about 0.5-1 m at the same moment, treat them as one, using assignment matching (Hungarian algorithm) on a distance cost. No face or appearance model needed.

#### **Non-overlapping cameras**

* Use a handoff zone where one camera's exit area meets the next camera's entry area, plus time gating (someone leaves A toward B, and someone appears in B within 10 seconds).  
* Add a clothing-appearance embedding (never faces) only as a tiebreaker, kept in memory for the visit and deleted on exit.

#### **Single camera still needs stitching**

A track that breaks and restarts after an occlusion creates a second person. Short-gap stitching (same area, similar velocity, gap under a few seconds) is simpler than cross-camera matching and still necessary.

# **Groups and parties**

Infer parties by behaviour. Nobody has to carry anything.

* Seed a party at the entrance, where people arrive together.  
* Keep the link while members stay within about 2 m with similar velocity.  
* Allow soft splits and merges. A child runs ahead, a parent goes to another aisle: membership stays, with a currently separated flag.  
* Count dwell per person (person-seconds). Count visits and conversion per party.  
* Group inference will sometimes be wrong, so show it as probable.

#### **If tokens must be linked within a group**

* **Co-issuance:** a staff tap or kiosk at the entrance assigns tokens a shared party ID (group of 4, 2 tokens).  
* **Token-to-token proximity logging:** each token records nearby token IDs over time. Covers token holders only.  
* **Do not impute:** a lost or unreturned token is a missing sample. Flag the visit as no token data and fall back to CCTV.

Bags and carts are optional anchors that raise confidence, never the identity. A family with three bags is still one party. A cart parked at an aisle end is not dwell.

# **Checkout attribution**

The cleanest identity anchors in a store are the entrance and the register. A party seen at a checkout lane at a given time can be matched to the POS transaction timestamp, with no phone or tag.

#### **Retroactive value attribution**

1. Track the walker anonymously through the store with a short-lived track.  
2. At the register, match the party to the POS transaction by timestamp and lane.  
3. Attribute the basket value back to the path they took.

Heat maps can then be weighted by **revenue per visitor** instead of footfall. Examples: shoppers who passed through zone C spent 30% more on average, or this aisle gets traffic but high spenders skip it.

#### **Limits**

* Needs an unambiguous match, so queue overlap hurts.  
* Correlation, not causation.  
* Needs a POS timestamp feed, mocked in the demo.  
* Browsers who buy nothing never match. You still get their path and dwell, which is the visited-but-did-not-buy group.  
* Requires a register visible in frame. Check the footage before committing.

# **Merging heatmaps**

Keeping sources separate and merging at the end is right. Weighting people who carry both a phone and a bag more heavily is not, because it biases the map toward big-basket shoppers with an app.

#### **Better rule: weight by source reliability per location**

* CCTV is the unbiased base since it sees everyone. Where it is weak (occluded corners, far from camera, poor light), give other sources more weight there.  
* Use people seen by both sources to **calibrate**, not to dominate. If carts are detected 30% less in back aisles because of metal shelving, learn that correction factor and apply it.  
* Normalize each source before merging. A cart heatmap shows where carts go, so scale it to all shoppers using the ratio of carts to CCTV-detected people per zone.

Matched pairs double as an accuracy measurement: how far apart the two sources' positions are.

# **Footstep paths**

Dotted paths from the entrance across sections, with density.

* **Space dots by time, not distance.** One dot every 2 seconds. Wide spacing means walking, tight clusters mean browsing, so dwell is visible without a separate chart.  
* **Smooth before drawing.** Raw positions jitter. A Kalman or similar smoother makes paths look intentional.  
* **Do not draw everything at once.** Past 20-30 paths it turns to spaghetti. Show a sample of individual journeys plus an aggregated flow layer where thickness or opacity encodes how many people took the route.  
* **Filters:** time of day, party, paths that visited zone X.  
* Label output as probable journeys with a confidence figure.

# **Sensor options**

Only CCTV, PIR, mmWave and overhead stereo can see the anonymous walker. Of those, only CCTV matches the brief and costs nothing in hardware. Everything that carries identity (phones, tokens) adds sampling bias, and everything with precise position (stereo, tokens) needs new hardware. That is why CCTV is the base and the rest are layers.

> Accuracy figures in these tabs are typical ranges from general knowledge, not measurements. Validate them on your own footage before using them in a pitch.

# **Alternative sources**

| Source | What it gives you | Trade-off |
| ----- | ----- | ----- |
| Wi-Fi / Bluetooth sniffing | Rough device positions, repeat visits | Low accuracy (3-10 m), only phone owners, privacy and MAC randomization |
| BLE beacons / app positioning | Accurate location for app users | Only works for customers who installed the app |
| Overhead people-counting sensors (thermal, time-of-flight, stereo) | Very accurate counts at entrances and aisle ends | Hardware cost, counts only, no journeys |
| Smart carts / baskets (RFID or UWB) | Precise paths and dwell, linked to cart contents | Needs special carts |
| POS / loyalty data | What was bought, when, basket composition | Nothing about behaviour before purchase |
| Door counters | Total footfall | Nothing inside the store |
| Shelf / weight sensors | Pick-up events at product level | Expensive, installed per shelf |

# **Positioning comparison**

### **Table 1: capability**

| Approach | Covers token-less, phone-less walkers? | Typical position accuracy | Identity persistence | Metal shelf / occlusion | Real-time? |
| ----- | ----- | ----- | ----- | ----- | ----- |
| Mobile GPS | Only app users; GPS fails indoors | Poor indoors (tens of metres) | Strong, if opted in | Very poor | Yes |
| Passive Wi-Fi/BLE sniffing | Only people carrying a phone | 2-5 m (RSSI), worse near metal | Weak (MAC randomization) | Poor | Yes |
| In-app BLE beacon positioning | Only app users | 1-3 m with dense beacons | Strong | Moderate | Yes |
| Existing CCTV \+ CV | **Yes** | About 0.5-1 m in central view, worse at distance and when occluded | Short tracks, stitching needed | Occlusion is the main weakness | Yes |
| Overhead stereo cameras (RetailNext-style) | **Yes** | About 0.2-0.5 m | Short tracks | Low occlusion | Yes |
| Passive RFID checkpoint tokens | Only token holders | Zone-level, at reader points | Strong at read points | UHF hurt by metal and bodies | Yes |
| Offline IMU logger token | Only token holders | Drifts, needs landmark correction | Strong per token | Immune to metal (no signal needed) | **No**, data arrives after exit |
| Shelf PIR / ToF sensors | **Yes**, anonymously | Per-shelf presence | None | Little occlusion | Yes |
| mmWave radar | **Yes**, anonymously | Presence and counting by area | None | Reflections off metal need testing | Yes |
| POS checkout match | Only at checkout | n/a (a timestamp anchor) | Strong at one point | n/a | Yes |

### **Table 2: practical fit**

| Approach | Bias introduced | Privacy / legal risk | Setup cost | Fits the brief? |
| ----- | ----- | ----- | ----- | ----- |
| Mobile GPS / app | Heavy: app installers only | High (consent, DPDP) | App development | No |
| Passive Wi-Fi/BLE | Phone carriers only | High | Low to moderate | No |
| CCTV \+ CV | Low | Low if no video stored | Low (hardware exists) | **Yes** |
| Overhead stereo | Low | Low | High (new hardware) | No |
| Passive RFID tokens | Compliance and loss | Moderate | Moderate | No |
| IMU logger tokens | Compliance and loss | Moderate | Moderate to high | No |
| Shelf PIR | None | Very low | Low per unit | Partly (add-on) |
| mmWave | None | Very low | Moderate | Partly (add-on) |
| POS match | Purchasers only | Low | Low (data feed) | Yes (data source) |

# **Phones and Bluetooth**

#### **Why phones are dropped as an identity source**

* GPS does not work reliably indoors.  
* Modern iOS and Android randomize Wi-Fi and BLE MAC addresses, so passive sniffing cannot give a persistent identity.  
* A person may carry several devices. Several people may share one, or leave devices behind.  
* Signal strength and visibility vary, and metal shelving blocks Bluetooth.  
* Collecting device identifiers raises privacy concerns and needs consent under the DPDP Act.  
* The only reliable route is an opt-in app, which most walk-in shoppers will not install.

At most, keep phones as a crowd-density hint. Put this limitation section in the README.

# **Tokens and carts**

#### **Cart or bag tags**

* **Better:** persistent identity, no occlusion, simpler privacy, works through shelves and blind spots (for Bluetooth, within limits).  
* **Worse:** selection bias (many shoppers carry nothing), a cart is not a person (parked carts fake dwell), RSSI error of 2-5 m, still needs a store map, cost and battery upkeep, and it is off-brief.

#### **The physical token handed out at the door**

GPS will not work indoors. A copper coil alone is an antenna as in RFID or NFC: passive, no memory, only responds near a reader. Two workable versions:

| Version | How it works | Strength | Weakness |
| ----- | ----- | ----- | ----- |
| A. Checkpoint tags (passive RFID) | Readers at zone or aisle entrances log each token as it passes | Cheap, no batteries, exact zone sequence | Dwell only inferred between reads; metal and bodies interfere with UHF |
| B. Offline logger (battery, IMU, flash) | Records steps and heading by dead reckoning; a scanner dumps and resets it at exit | No receivers in store, emits nothing | Drift needs landmark correction, orientation changes in a pocket, no real-time heat map |

#### **Why it struggles for the baseline shopper**

* Compliance bias: people refuse, forget, drop it in a basket, or leave with it.  
* Entrance friction, hygiene and staffing cost.  
* Hardware pool, charging, loss and damage. Even at a few hundred rupees per unit it adds up.  
* Off-brief, since the statement asks for existing cameras.  
* A token tied to a person at the door is still a per-visit identifier, so consent and a reset policy are needed.  
* Children cannot hold tokens, so one family produces one token for several people.

#### **Where it is genuinely useful**

1. **Ground truth for the accuracy slide.** Give tokens to 10-15 friends in the mock-store recording and compare logged paths with CCTV tracks.  
2. **Checkout linking** for token holders, making revenue-per-path exact for that sample.  
3. **Premium upgrade tier:** works on existing cameras alone and gets sharper with optional tokens.

Recommendation: keep camera-only as the product. Treat tokens as validation and a roadmap item. Building Version B takes real firmware work (an ESP32-class board with an IMU, offline logging and a reader script).

# **Other sensors**

| Approach | What it gives you | Tradeoff |
| ----- | ----- | ----- |
| Shelf-level PIR or time-of-flight (a few dollars each) | Anonymous someone-is-at-this-shelf per section, unaffected by occlusion | No identity, presence only |
| mmWave radar (60 GHz modules) | Privacy-safe presence, motion and counting in aisles or queues, works in the dark | Metal shelves reflect; placement needs testing |
| Low-res thermal arrays (8x8, 32x24) | Cheap anonymous counting at entrances and chokepoints | Low resolution, short range |
| Wi-Fi sensing (CSI) | Device-free motion detection using existing access points | Research-grade, coarse, hard to demo reliably |
| Cart-mounted IMU with dead reckoning | Path with no receivers, corrected at landmarks | Drift, and covers carts only |
| Opt-in QR check-in (for example a coupon at the door) | A consented sample with ground-truth journeys | Small sample, needs shopper action |
| Existing store data: door counters, POS timestamps, Wi-Fi AP client counts | Free anchors for entry counts and checkout matching | Coarse, store-wide |

Cheapest and most useful for a hackathon: a few **shelf PIR sensors on an ESP32**. They give independent ground truth for was someone at this section, and for how long, and they directly confirm dead zones: no PIR triggers plus no CCTV dwell is a very confident dead zone.

# **RetailNext**

> From general knowledge of their public material. Verify against their site before quoting it in a pitch.

#### **How they track foot traffic**

* **Overhead sensors:** ceiling-mounted video sensors, with stereo (two-lens) 3D cameras at entrances and key areas. Depth makes counting from above far more reliable than a single wall camera.  
* **Sensor fusion:** Wi-Fi and BLE signals from phones, RFID and POS data combined in one cloud platform.  
* **Dedicated hardware:** accuracy comes from purpose-built, calibrated sensors.

#### **How they get the map**

They mostly do not do it without a blueprint. The retailer supplies a CAD or PDF floor plan, installers place sensors on it and calibrate each to known points. For a chain this effort is amortized across many stores with one template.

#### **Our gap**

Comparable insight from cameras the store already owns, which were mounted for security, not measurement, with self-serve calibration for stores that have no clean floor plan.

# **Product branches**

Each branch answers who acts on the output and what they do next. Pick one spine and one supporting branch. Trying all five gives a wide, shallow demo.

| Branch | Intent | Demo moment | Main risk |
| ----- | ----- | ----- | ----- |
| 1\. Store manager's diagnostician | Tell a manager why a section underperforms and what to try, in plain language | Aisle 4 end-cap: visits 35% below usual, dwell normal, so people are not seeing it. Move signage. | Recommendations feel generic without concrete rules; needs baseline history, so seed it |
| 2\. Layout experiment engine | Turn we moved the display into measured evidence, normalized for total traffic | Two clips of one mock store with a display moved: \+22% visits to the promo zone after normalizing | Needs a consistent camera and two comparable recordings |
| 3\. Live operations console | Real-time awareness: checkout wait is about 6 min, open another counter | A queue builds in the clip and an alert fires early | Closest to existing vendors; depends on tracking in crowds |
| 4\. Conversion funnel | Split each zone into passed, stopped, bought so managers know which fix applies | Two zones with equal sales and very different funnels | POS data is mocked, so judges may discount it |
| 5\. Privacy-first edge kit | Deployable by a small retailer: cheap hardware, existing cameras, under an hour | Calibrating a new camera live in a couple of minutes | Setup UX is easy to underinvest in; weak if analytics beneath are shallow |

#### **Combinations**

| Spine | Best supporting branch | Why |
| ----- | ----- | ----- |
| Branch 2 (experiments) | Branch 1 (diagnostician) | Measure the change, then explain it |
| Branch 1 (diagnostician) | Branch 4 (funnel) | Explanations sharpen when you can say stopped but did not buy |
| Branch 3 (live ops) | Branch 5 (edge kit) | A real-time story only works if it is cheap to deploy |

#### **Current recommendation**

**Branch 2 as the spine, Branch 1 as support.** Existing tools show a heat map, but few say whether a change worked. It is very demoable, since you control both layouts in a mock store and have ground truth. Branch 1's explanations make the result readable to a non-technical manager. It also has a business model: suppliers who pay for display space want proof of exposure.

Branch 3 is the safest if time is short, but is easiest to dismiss as RetailNext but worse.

Before and after comparison must be normalized for total store traffic, otherwise a busy Saturday looks like a successful move.

# **Validation and accuracy**

Most hackathon CV demos show overlays and no accuracy numbers. One accuracy slide separates this project from those.

#### **Plan**

1. Manually annotate 2-3 minutes of the clip: when each person enters and leaves each zone.  
2. Report dwell error, count error and ID switches.  
3. Record a taped-off mock store for ground truth and a controlled camera angle. Retail footage that is public is scarce.  
4. Script friends to act out waiting and browsing so loiterer detection has ground truth.  
5. Optional: give a few friends tokens or place shelf PIR sensors as independent ground truth.  
6. State accuracy honestly as zone-level, not centimetre-level.

#### **Metrics to report**

* Dwell error per zone (seconds and percent)  
* Count error at entrance and checkout  
* ID switches per minute at the chosen fps  
* Journey completeness (share of visits stitched entrance to checkout)  
* Mission and loiterer classification accuracy on scripted clips

# **Build plan**

1. Detect and track people on a sample video and draw IDs  
2. Homography and floor-plan plotting  
3. Zones and dwell times  
4. Heat map  
5. Bottleneck and dead-zone logic  
6. Dashboard and recommendations  
7. Demo polish

The right cut depends on hackathon length and team size. See Open questions.

# **MVP cut**

| Tier | Items |
| ----- | ----- |
| Must have | Detect and track, homography, zones with dwell, decaying heat map, one bottleneck rule (checkout throughput), one relative dead-zone rule with a plain-language flag |
| If time allows | Before and after comparison, Sankey, staff filtering, LLM store-manager brief, mission segmentation, two dwell metrics (presence and engaged), loiterer segment |
| Pitch only | Multi-camera fusion, POS integration beyond a mock, what-if simulation, tokens and BLE, appearance Re-ID |

#### **Tiers for identity work**

1. **Tier 0:** per-camera tracking, floor coordinates, zone dwell, heat map.  
2. **Tier 1:** dedupe across overlapping cameras by spatial gating.  
3. **Tier 2:** party clustering by co-movement, with confidence.  
4. **Tier 3:** BLE anchors, POS time-matching, appearance Re-ID. Pitch only unless time allows.

A single camera, or two overlapping ones, with Tiers 0-2 and a measured accuracy slide beats a five-source fusion that was never validated.

# **Demo and data**

A 5-10 minute clip (public footage or a taped-off mock store), fed through the same streaming loop and throttled to real time. Only the source URL changes for a real camera.

#### **Minimum scene**

* A checkout lane in frame, if the revenue-per-path feature is to be built rather than described.  
* At least two zones with different product types, for the dwell-versus-purchase split.  
* A mock POS basket file where each item carries a section tag.  
* Two layouts of the same space, if Branch 2 is the spine.  
* Scripted actors for browsing, directed shopping and waiting.

# **Limitations and roadmap**

#### **Honest notes**

* Occlusion and ID switches happen when people cross paths. We report this rather than hide it.  
* Single camera for the prototype. Multi-camera stitching is future work.  
* CCTV shows behaviour, not purchases. Conversion needs POS data, mocked in the demo.  
* Attribution from path to purchase is correlation, not causation.  
* Journeys are probable, not certain, and carry a confidence figure.  
* Commercial products exist. Our edge is affordability, privacy, relative flagging and actionable output.

#### **Roadmap**

* Multi-camera fusion  
* POS and loyalty integration  
* Layout what-if simulation  
* Time-of-day comparisons  
* Engagement versus passing-by using head orientation  
* Optional anchors (tags, PIR, mmWave) that raise confidence without being required

# **Open questions**

Answers here decide what is built and what stays a design slide.

| Question | Why it matters | Answer |
| ----- | ----- | ----- |
| How long is the hackathon, and how many people are on the team? | Sets the line between must-build and pitch-only | Not yet known |
| Is there candidate footage? What is the camera angle and lens? | Decides calibration difficulty and whether homography is workable | Not yet known |
| Does the footage include a checkout, and how many cameras overlap? | Decides if revenue-per-path and Tier 1 dedupe are built or described | Not yet known |
| Record a mock store or use public clips? | Mock store gives ground truth and two comparable layouts | Not yet known |
| Does anyone have embedded hardware experience, and are ESP32 boards available? | Decides if PIR or token validation is realistic | Not yet known |
| Which branch is the spine? | Shapes MVP, demo and pitch | Leaning Branch 2 with Branch 1 |

