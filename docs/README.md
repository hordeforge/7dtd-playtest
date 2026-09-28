# docs/

Design and decision records for `7dtd-playtest`. Each one states its Status
at the top: what is implemented, and which acceptance criteria still need a
real client or a human watching the evidence.

| Document | Subject |
|---|---|
| [INGAME_VIDEO_CAPTURE.md](INGAME_VIDEO_CAPTURE.md) | `CaseDef.StagedClip`, the on-demand clip recorder, and `scripts/capture_video.sh`: in-engine motion evidence, no desktop grab |
| [VIDEO_MODEL_FEEDBACK.md](VIDEO_MODEL_FEEDBACK.md) | `scripts/video_review.py` / `review_video.py`: an advisory vision-model critique of a clip, through the deadeye gateway |
| [ASSET_VIDEO_FEEDBACK_LOOP.md](ASSET_VIDEO_FEEDBACK_LOOP.md) | Routing a reviewed clip back to the asset iterator and to shamway's generation defaults |
| [THREAT_MODEL.md](THREAT_MODEL.md) | What the host scripts trust, and the gaps in that trust |

`DST.md` and `SCENARIOS.md` sit at the repo root and cover the lock's
deterministic simulation and the case catalog respectively.
