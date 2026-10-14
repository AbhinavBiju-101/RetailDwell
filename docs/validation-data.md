# Validating the camera pipeline on public data

Goal: replace the proposed accuracy targets in `presentation.md` with measured numbers, without recording a store first.
What the pipeline produces is floor positions in metres per person (`track_id, t, x, y`), so the best public data has
ground-truth positions on the floor plane, not only boxes in the image. Researched 11 Oct 2026; check each licence yourself before use.

## Shortlist

| Dataset | What it is | Ground truth | Fit | Access and terms |
|---|---|---|---|---|
| MMPTRACK (Microsoft, ICCV 2021 workshop) | About 6.5 h (5 h train, 1.5 h validation; the paper says 9.6 h with the hidden test split) of 15 fps frames at 640x320 from calibrated, overlapping cameras in 5 environments: retail, lobby, industry, cafe, office. The retail scene has 6 cameras | Per-camera boxes with person ids, and person footpoints on the floor grid in world coordinates (`topdown_[frame].csv`: id, x, y) | Best fit: floor-plane ground truth, a retail scene and calibration. Caveat: the "retail" scene is a staged set in Microsoft labs with 28 paid participants, so it tests tracking and zone/dwell accuracy but not real shopper behaviour or real store clutter. Frames are low resolution | Research use only. Request by emailing a signed user agreement (link on https://iccv2021-mmp.github.io/subpage/dataset.html). Do not ship or redistribute it; use it for evaluation |
| NVIDIA PhysicalAI-SmartSpaces / AI City Challenge | Synthetic multi-camera indoor scenes (Omniverse), mostly warehouses; the 2024 edition of the people-tracking track included a retail-style scene, later editions focus on warehouses | World-coordinate boxes and ids, calibration, top-down maps | Good for stress-testing floor projection and ids, with known geometry. Synthetic, and mostly not retail | On Hugging Face (`nvidia/PhysicalAI-SmartSpaces`); read the licence on the card. Real-world test scenes released in 2026 are warehouses |
| WILDTRACK (ETH) | 7 calibrated cameras on an outdoor walkway, 313 people, ground-plane annotations, 2 fps | Ground-plane positions | Cheap check of ground-plane error and id switches. Outdoor, so it says nothing about shelves or occlusion | Academic dataset; check terms |
| Sunmi head-detection and tracking (supermarket sample) | Annotated in-office clips plus a sample from a 24-hour supermarket video, head boxes | Boxes and track ids, no floor coordinates | Real store footage, but you would have to calibrate the floor yourself and annotate stops by hand | https://github.com/Sunmi-AI-Lab/head-detection-and-tracking (paper arXiv:2005.08009); check the licence |
| RetailS / PoseLift | Real store CCTV turned into anonymised pose sequences (6 indoor cameras) for shoplifting research | Tracks come from ByteTrack, not human annotation; no floor coordinates | Not a validation set for tracking accuracy. Useful only as a source of realistic movement patterns | GitHub `TeCSAR-UNCC/RetailS`; read the terms |

## Recommended plan

1. Request MMPTRACK now (it is an email and a signed form; the wait is the slow part).
2. Score it with `python -m agent evaluate` (built and tested on the synthetic scene): it reads ground-truth floor tracks and the agent's output, and reports position error, identity switches, fragments per person, zone agreement, per-person stop and dwell error, and the stopped share per zone as the app would show it.
   For MMPTRACK use `--truth-format mmptrack --voxel-m <from the calibration file>`. The loader follows the documented file format but has not seen the real files, so plot a few tracks over the scene before trusting a score.
3. Run the agent with YOLO on the MMPTRACK retail frames (they are image frames, so write them to a video or add an image-folder source), with the provided calibration for the floor projection.
4. Add one cheap real-world check: a short clip you record yourself with tape marks on the floor, which tests what the staged data cannot (real occlusion, lighting, your camera height).
5. Put the measured numbers and their conditions (staged scene, 6 cameras vs the agent's single camera) into `presentation.md`. State plainly that MMPTRACK is staged.

Caveats to keep honest: MMPTRACK has 6 overlapping cameras and the agent is single-camera, so the agent will be tested on one view at a time and
the result is a lower bound for an occluded retail floor. No public dataset here pairs real store video with till data, so the sales matching
can only be validated on simulation (done: 95.7% to 100% of usable matches correct) until a pilot store provides a POS export and a camera.

Update: track stitching is built and measured on the synthetic scene (see `docs/context.md`). On MMPTRACK, score with and without `--no-stitch` and report both, because the stitching settings were tuned on drawn people.
