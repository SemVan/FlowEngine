# Roadmap

Three phases. Each phase has clear exit criteria. Don't blur the lines.

## Phase 1 — "the box jogs" *(this release)*

Goal: jog every axis safely, in mock or real, with full audit trail.

Delivered:
- Marlin checksum + resend handshake from day one.
- Mock transport mirroring Marlin semantics (errors + disconnects injectable).
- Command queue with per-class timeouts and `M410` abort.
- Controller state machine.
- Soft limits enforced in software.
- REST endpoints: jog / move / home / stop / state / diagnostics / config.
- WebSocket telemetry for state + position + log.
- Single-page web UI with per-axis jog panel, status, log, big red Abort.
- Default configs for all 8 axes; 5 sample procedures (lint-only).
- Docs: README, INSTALL, ARCHITECTURE, HARDWARE, GCODE_PROTOCOL, PROCEDURES, ROADMAP, STEPSKIP.
- Unit + integration tests against MockTransport.

Exit criterion (met): operator can `pip install -e .`, run `./scripts/run_mock.sh`, jog every axis in the browser, hit Abort mid-move, see a clean recovery, with all tests green.

## Phase 2 — procedures + modes + fluidics view

Goal: turn jogs into named, run-with-one-click procedures and reflect them visually.

Scope:
- **Procedure runner** (`flowengine/procedures/runner.py`):
  - Walk steps; dispatch to `GcodeSender` / `Valve` / `Pump`.
  - Pause / resume / abort.
  - Per-step WS telemetry (current step index, log).
- **Mode manager** (`flowengine/modes/manager.py`):
  - Switch the active mode at runtime.
  - Apply `runtime_overrides` (e.g., lower `motion.feedrate_cap` in wash mode).
  - Filter procedures shown in the UI by `modes[].procedures`.
- **Fluidics SVG live binding** (`web/static/js/panels/fluidics.js`):
  - Map valve state → CSS class on the SVG `<circle>` element.
  - Map pump activity → CSS class on the `<rect>`.
  - Highlight active path segments.
- **Config editor in UI**: form-driven edit of `runtime.yaml` (not device map yet — that's a bring-up edit, not a runtime knob).

Exit criterion: operator can switch modes, pick a procedure, run it end-to-end, watch the SVG light up, abort cleanly mid-procedure.

## Phase 3 — pressure + step-skipping test

Goal: close the loop on motion confidence by measuring step loss vs pressure.

Scope:
- **Pressure sensor** beyond `Manual` / `Mock`:
  - Decide source (controller M105 / separate USB / I²C through Monster8 AUX) — coordinate with hardware partner.
  - Implement `ControllerSensor` (or `USBSensor`).
  - Add `pressure` channel to WS telemetry.
- **uPlot streaming chart** (`web/static/js/panels/pressure.js`):
  - Last N minutes of samples.
  - Threshold markers configurable.
- **`wait_pressure` step primitive** (already in DSL; runner support).
- **Step-skipping test** (`flowengine/experiments/stepskip.py`):
  - Parameterized ramp (feedrate or load).
  - For each setpoint: command N steps; record commanded vs reported position; record pressure.
  - Declare step loss when delta > tolerance.
  - Emit CSV + Markdown summary with the first-loss setpoint.
- **Test runner UI** (`web/static/js/panels/stepskip.js`).

Exit criterion: operator can run the step-skip test on one axis, get a CSV and a chart, and act on the result by adjusting `feedrate_max` in the device map.

## Out of scope (for now)

- Auth / multi-user.
- Klipper transport (stub raises NotImplementedError with migration pointer).
- LAN binding without auth (flag exists, docs say "don't").
- LIMS / cloud integration.
- DSL features beyond the primitive set in `PROCEDURES.md`.
