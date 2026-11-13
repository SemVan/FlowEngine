## 2026-10-12 — v8.1 endstop timeout fix

- Removed the accidental 0.5 s serial timeout cap from X/Y physical calibration.
- Routine M410 quickstop after a physical endstop hit now has a >=2 s acknowledgement window (or the configured diagnostics timeout).
- Timeout errors include the exact G-code command that failed to answer.
- Added regression coverage for delayed M410 acknowledgement without forcing reconnect.

## 2026-10-12 — continuous X/Y seek + live Home UI

- Replaced stop/start X/Y calibration and Home stair-stepping on real Marlin with one continuous G1 per seek phase plus live M119 polling.
- Routine endstop capture uses in-band M410 and immediately resynchronizes the unreferenced axis from Marlin Count / M114 R; normal endstop hits no longer require reconnect.
- Home exposes live phase progress and a graceful “Остановить Home” control.
- Calibration can switch X ↔ Y while idle; unsaved per-axis drafts survive the switch and completed drafts can be saved together.
- Fast seek → release/backoff → slow latch remains the shared primitive for Calibration and Home.
- Added regression coverage for continuous Marlin seek and X/Y draft switching.

## 2026-10-12 — physical X/Y homing and reference state

- Calibrated X/Y Home no longer uses blind `G28`; it uses the saved physical direction map and shared endstop input.
- Home sequence is `fast seek → release/backoff → slow latch → G92 reference → pull-off`.
- `axis_calibration.home_side` persists the physical reference side (`motor` by default).
- Logical X/Y coordinates are assigned from the measured physical stroke: the two switches correspond to `0` and `travel` without turning that span into a software motion limit.
- X/Y normal motion still requires a valid runtime reference; calibration survives reconnect, reference does not.
- Home stores the Marlin `Count` seen at the precise latch for future step-loss checks.
- UI now shows calibration/reference state and provides Home X/Y independently of the normal motion interlock.
- A shared endstop already active at process start is treated as side-unknown; Home refuses to guess which physical edge is pressed.
- Closed old transport test debt: `Resend: 0` now performs explicit M110 resynchronization and the default serial port is cross-platform `auto`.

## 2026-10-06 — physical X/Y calibration

- Removed the legacy manual X/Y working-range wizard and host-side guessed X/Y limits.
- Added one physical-endstop calibration flow for X/Y: label motor/far, learn +/- direction, measure the full endstop-to-endstop span and record Marlin Count when available.
- X/Y profiles start with `travel: null`; measured geometry lives in `axis_calibration` after commissioning.
- Normal motion requires a trustworthy reference; calibration remains available while `motion.enabled=false`.

# Changelog

All notable changes to FlowEngine will be documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Phase 1 — "the box jogs" (initial release)

Added:
- Project scaffold: `flowengine/` package, `web/` (templates + vanilla-JS), `config/` YAML defaults, `docs/`, `tests/`.
- Pluggable transport layer: `MarlinTransport` (line-numbered checksum framing + resend), `MockTransport` (mirrors Marlin semantics including injected errors/disconnects), `KlipperTransport` stub.
- Command queue with single-line `ok` handshake, per-command-class timeouts, abort via `M410`.
- Controller state machine (`disconnected | connected_idle | homing | moving | aborting | errored`).
- REST endpoints for jog / move / home / stop / state / config / diagnostics.
- WebSocket telemetry channel for position, state transitions, log lines.
- Reference-state motion validation plus physical-endstop X/Y calibration for wired-OR endstops.
- Endstop homing strategy. Sensorless / crash homing stubs raise `NotImplementedError`.
- Vanilla-JS frontend with jog panel, status, log tail, abort.
- Default config templates for all 8 axes; profile mechanism.
- Docs: ARCHITECTURE, HARDWARE, PROCEDURES, INSTALL, GCODE_PROTOCOL, ROADMAP, STEPSKIP.
- Unit + integration tests against MockTransport.

### Phase 2 — procedures + modes + fluidics view (planned)

### Phase 3 — pressure sensor + step-skipping test (planned)

## 2026-10-06 — commissioning reconnect fix
- Hand-drive hold-to-run now uses bounded completed motion segments instead of M410 on normal button release.
- Added core `/api/reconnect` for browser/BAT launches; no desktop wrapper is required.
- The global reconnect button is now available in both desktop and browser modes.
- Reconnect explicitly loses coordinate references and never replays a movement command.
