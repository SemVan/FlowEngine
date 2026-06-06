# Hardware bring-up

This is the document you fill in when bringing up a new instrument. **None of the placeholders in `config/device_map.yaml` are guaranteed to match your hardware** — they're plausible starting points only.

## Axis assignment

| Marlin axis | Physical device       | Notes |
|-------------|-----------------------|-------|
| X           | autosampler X         | wired-OR endstops |
| Y           | autosampler Y         | wired-OR endstops |
| Z           | autosampler Z         | `home_direction: max` (top is safe) |
| E0          | sample syringe        | wired-OR endstops at both ends |
| E1          | sheath syringe        | wired-OR endstops at both ends |
| E2          | peristaltic wash pump | encoder feedback (Phase 3) |
| E3          | sample-line valve     | 2-position, 90°, wired-OR endstops |
| E4          | sheath-line valve     | 2-position, 90°, wired-OR endstops |

If you reassign physical slots, edit `config/device_map.yaml` — never patch Marlin's `Configuration.h` to "fix" polarity or direction. Keep the firmware vanilla; the config file is the single source of truth.

## TODOs to resolve at bring-up

| Field | Where | How to determine |
|---|---|---|
| `steps_per_unit` per axis | `device_map.yaml` | Calculate from leadscrew pitch × driver microsteps. Verify with a jog of known distance + caliper measurement. |
| `travel` per axis | `device_map.yaml` | Mechanical: measure the available stroke. Subtract a small safety margin. |
| `home_direction` per axis | `device_map.yaml` | Choose the side where the mechanical stop is reliable and where homing is *safe* (Z homes up, away from samples). |
| `dir_invert` per axis | `device_map.yaml` | Issue a tiny `+1` jog with hand on the coupling. If the axis moves the wrong direction, set `dir_invert: true`. |
| `feedrate_default` / `feedrate_max` | `device_map.yaml` | Start low. Raise until you see step loss or audible distress. Step-skip test (Phase 3) will help formalize this. |
| Endstop polarity (`endstop_inverted`) | `device_map.yaml` | Run `M119` via `/api/diagnostics/endstops` with switches open and pressed; if "triggered" / "open" are swapped, invert. |
| Driver type (TMC2209/5160 vs A4988/DRV8825) | unknown today | Confirm with the hardware partner before enabling sensorless homing. |
| Pressure sensor source | `runtime.yaml: pressure.source` | Default Phase 1 is `manual`. Phase 3 will offer `controller` once wiring is decided. |
| Marlin firmware features | `runtime.yaml: firmware.features_required` | The connect probe (`M115`) will refuse to start if a required feature isn't advertised. Add only features you've confirmed present. |

## Bring-up checklist (one axis at a time — **do not batch**)

For each newly-wired axis:

1. **Firmware probe.** Open the UI → click "Read firmware". Confirm `FIRMWARE_NAME:Marlin 2.x` and the advertised capabilities. If `CHECKSUM` isn't listed, recompile Marlin with checksum support.
2. **Endstop sanity.** Click "Read endstops". Manually trigger the switch with a finger. The triggered indication should change. Note **which** axis goes hot — that's your wiring map (the wired-OR makes hardware-side disambiguation impossible). Record in this file.
3. **Low-speed jog with hand on the coupling.** Hand on coupler. Lowest `feedrate_default`. Issue `+0.1 mm` jog. Confirm direction. If wrong, set `dir_invert: true` and retry. **Do NOT skip the hand check** — a polarity-inverted Z can crash hard against the bed.
4. **Home one axis.** Click "home" on that row. Watch for trigger, back-off, re-home. Time it. If it overshoots, the back-off distance in Marlin firmware is wrong (raise `HOMING_BUMP_MM` for that axis).
5. **Soft-limit test.** From the UI's "config" panel, read the current `travel`. Issue a jog that would exceed it. The server should reject with HTTP 400 (`soft-limit: ...`) before any G-code is sent.
6. **Abort test.** Issue a long slow move (e.g., full stroke at low feedrate). Hit the Abort button. `M410` is sent; motion should halt within ~0.2 s. State returns to `connected_idle`.
7. **Disconnect test.** Unplug USB mid-idle. UI should drop to `disconnected` within ~2 s (driven by read-EOF + heartbeat). Replug. State should not auto-resume — motion is blocked until you re-home.
8. **Repeat per additional axis.** Don't batch bring-up. Each axis gets its own pass.

After all eight axes are passing this checklist, commit a copy of the populated `device_map.yaml` as a comment in this file or under `docs/bringup/YYYY-MM-DD.md`.

## Endstop wiring — wired-OR

Two physical switches per axis are wired so that triggering either pulls the controller's single input low. Consequences:

- The controller cannot tell which side fired. **Don't ever ask.**
- Soft limits (`travel`) are enforced in software (`MotionModel`) — they're the only authoritative limit.
- Homing always goes toward `home_direction`. Reaching the opposite end via runaway is prevented by `travel`-bounded soft limits and abort.
- Marlin sees one endstop per axis (configured as `min`); we never use Marlin's "max endstop" feature.

## Homing strategy

Three are scaffolded; only `endstop` is implemented for Phase 1.

| Strategy | When to use | Status |
|---|---|---|
| `endstop` | Default. Mechanical switch wired-OR per axis. | **Implemented.** |
| `sensorless` (StallGuard) | Only if drivers are TMC2209/TMC5160 *and* sensitivities are calibrated per axis. | Stub — `NotImplementedError`. |
| `crash` | Last resort, when no endstop is present. Requires per-axis safe-current calibration. | Stub — `NotImplementedError`. |

To enable `sensorless` later:
1. Confirm with hardware partner that drivers are TMC and DIAG pins are wired through.
2. Set per-axis `M914 X<sgthrs>` values experimentally — they're stiffness-, lubrication-, and temperature-sensitive.
3. Pick a homing current with `M906 X<mA>` low enough to stall quietly at the stop and high enough to actually move under load.
4. Implement `SensorlessHoming.home(...)` and set `homing_strategy: sensorless` per axis in the device map.

To enable `crash` later:
1. Determine the safe homing current per axis empirically — the lowest current that still moves the axis under no load. This must not damage the coupling on contact.
2. Implement `CrashHoming.home(...)`: store original current, switch to safe current, move further than `travel`, accept lost steps, switch back, `G92` to set zero.

## Wiring log (fill in at bring-up)

```
Date:
Operator:
Marlin version:
Driver type per slot:
  X:   ____  | Y:   ____  | Z:   ____
  E0:  ____  | E1:  ____  | E2:  ____
  E3:  ____  | E4:  ____
Endstop polarity (M119 with switch open / pressed):
  X_min: ____ / ____    | Y_min: ____ / ____    | Z_min: ____ / ____
  ...
Direction-inversion needed (true/false) per axis: ...
Notes / oddities: ...
```
