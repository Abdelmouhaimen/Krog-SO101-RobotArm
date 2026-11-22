# Behavior file and data contract

## File layout
`behaviors/<stable_uuid>.json` with schema version `1`. Keep raw capture and edits separate: use an immutable `source` record and an explicit `edits`/`derived` revision; do not overwrite original measurement points. Atomic writes (`temp` + rename) and robust validation; no DB or cloud.

## Required metadata
- `schema_version`, stable `id` (UUID), `name`, `description`, `created_at_utc`, `updated_at_utc`, `revision`, `origin` (`real_hand_guided`, `sim_sliders`, `sim_keyframes`)
- `robot_model`: SO-101; six exact joint names in M1 order, units (degrees for first five, normalized [0,100] for gripper)
- calibration identifier/fingerprint for real captures, M1 joint-coordinate mapping identifier, and source commit/model hash where available; prevent silently replaying stale calibration-specific data
- `samples`: strictly increasing relative monotonic `t_seconds`; each complete six-joint *position* source vector; `requested` and `sent` may be null, never imputed
- selection of canonical authoring series: physical hand guidance -> actual, sim keyframe/slider -> authored target; preserve provenance on every revision

## Validation and timestamps
- Start timestamp relative to first valid sample; capture wall-clock creation timestamp only as metadata.
- Exactly six finite joint values in canonical order/keys. No guessed values from previous command, no invalid/null *actual* physical sample.
- Physical sample gap `>0.250` seconds invalidates recording; threshold inclusive (`0.250` valid). Reject playable-save with interval detail.
- Trim may introduce boundary samples using linear interpolation; keep immutable originals and change log.
- Long preview/export files should include duration and sample count; no executable code or arbitrary commands inside behavior files.
- Editing out-of-range keyframes creates an effective derived trajectory clamped to M1 calibrated permitted bounds. Display per-joint before/after, affected timestamps and calibration fingerprint. Require new preview when edit, calibration or speed/loop settings change.

## Example *illustrative data only* (not real-world safe pose)
```json
{
  "schema_version": 1,
  "id": "1f7e0b95-7dde-4a04-903c-dfae9da3a982",
  "name": "wave",
  "description": "Hand-guided greeting",
  "created_at_utc": "2026-11-07T00:00:00Z",
  "updated_at_utc": "2026-11-07T00:00:00Z",
  "revision": 1,
  "origin": "real_hand_guided",
  "robot_model": "SO-101",
  "calibration_fingerprint": "example-only-not-valid",
  "joint_units": {"shoulder_pan":"deg","shoulder_lift":"deg","elbow_flex":"deg","wrist_flex":"deg","wrist_roll":"deg","gripper":"normalized_0_100"},
  "samples": [
    {"t_seconds": 0.0, "actual": {"shoulder_pan":0,"shoulder_lift":0,"elbow_flex":0,"wrist_flex":0,"wrist_roll":0,"gripper":0}, "requested": null, "sent": null},
    {"t_seconds": 0.05, "actual": {"shoulder_pan":1,"shoulder_lift":1,"elbow_flex":1,"wrist_flex":0,"wrist_roll":0,"gripper":1}, "requested": null, "sent": null}
  ],
  "edits": []
}
```
This example is a **shape illustration**, not approved physical targets. Exact schema should also define `source_asset_revision`, `preview_hash` and effective playback metadata once implementation chooses revision representation.

## Future tool compatibility
Behaviors identified by stable ID; rename must not alter ID. Pure internal operations (not MCP yet): list behaviors; preview by ID/revision; play one behavior by ID with speed/repetition args; stop current behavior; query state.

## Machine-readable contract
`contracts/behavior-v1.schema.json` validates the file shape. **The application must additionally enforce** strictly increasing timestamps, <=250ms adjacent recording gaps, real-source calibration fingerprint presence, immutable-source revision rules, numeric finite values, source-specific actual/requested provenance, and physical bounds. JSON Schema alone cannot prove timing integrity or hardware safety.

## Amendment (ADR-017, 2026-11-07): `settings.after`

```json
"settings": { "...": "...", "after": { "action": "rest | hold | pose", "pose_id": "<uuid> | null" } }
```

- Optional; absent means `rest`, so all earlier files are unchanged in meaning.
- `pose` requires a `pose_id` from the local pose library (`poses/<id>.json`). Real-arm poses carry the
  calibration fingerprint and are validated before use.
