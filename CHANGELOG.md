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
- Soft-limit enforcement in software (mandatory under wired-OR endstop wiring).
- Endstop homing strategy. Sensorless / crash homing stubs raise `NotImplementedError`.
- Vanilla-JS frontend with jog panel, status, log tail, abort.
- Default config templates for all 8 axes; profile mechanism.
- Docs: ARCHITECTURE, HARDWARE, PROCEDURES, INSTALL, GCODE_PROTOCOL, ROADMAP, STEPSKIP.
- Unit + integration tests against MockTransport.

### Phase 2 — procedures + modes + fluidics view (planned)

### Phase 3 — pressure sensor + step-skipping test (planned)
