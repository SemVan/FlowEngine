# FlowEngine

Control software for a flow cytometer's fluidics tract.

- Python 3.11+ backend (FastAPI + asyncio + pyserial).
- Vanilla-JS web frontend (no npm, no build step).
- Targets the **MKS Monster8 MK2** stepper-driver board, flashed with Marlin, over USB-CDC G-code.

```
┌─────────────────────┐    HTTP + WebSocket    ┌──────────────────────┐
│ Browser (vanilla JS)│ ◄───────────────────► │ FastAPI / uvicorn    │
└─────────────────────┘                        │ (state, queue, sender)│
                                               └──────────┬────────────┘
                                                          │ pyserial-asyncio
                                                          ▼
                                                ┌──────────────────────┐
                                                │ MKS Monster8 MK2     │
                                                │ Marlin firmware      │
                                                └──────────┬────────────┘
                                                           ▼
                                          syringes / valves / peristaltic / autosampler
```

## Quickstart (mock mode, no hardware)

```bash
git clone <this-repo> && cd FlowEngine

# Recommended: a virtualenv
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the test suite (mock-driven)
pytest

# Launch the app against a simulated controller
./scripts/run_mock.sh
# → open http://127.0.0.1:8765/
```

Without real hardware you get the full UI: jog any axis, watch positions update over WebSocket, hit Abort, simulate disconnect.

## Quickstart (real hardware)

1. Flash Marlin onto your Monster8 MK2 (see `docs/HARDWARE.md`).
2. Edit `config/device_map.yaml` — set `steps_per_unit`, `travel`, `home_direction` for each axis. Every placeholder there is marked `TODO(hardware)`.
3. Edit `config/runtime.yaml` if your serial port isn't `/dev/ttyACM0`.
4. `./scripts/run.sh` and open `http://127.0.0.1:8765/`.

If you've never wired this instrument before, follow the bring-up checklist in `docs/HARDWARE.md`. **Do not skip the endstop polarity test.**

## Documentation

| File | What it covers |
|---|---|
| `docs/INSTALL.md` | Install paths, USB permissions, common pitfalls |
| `docs/ARCHITECTURE.md` | Layered diagram, state machine, extension points |
| `docs/HARDWARE.md` | Wiring conventions, bring-up checklist, TODOs |
| `docs/GCODE_PROTOCOL.md` | Which M-codes we use, response handling, resend protocol |
| `docs/PROCEDURES.md` | DSL reference for `config/procedures/*.yaml` |
| `docs/ROADMAP.md` | Phases 1 / 2 / 3 and what each adds |
| `docs/STEPSKIP.md` | Phase 3 step-skipping test design |

## What's in this release (Phase 1)

- Serial transport with Marlin's checksum + resend protocol from day one.
- Mock transport with the same `ok` / error / disconnect semantics for CI.
- Command queue with per-class timeouts and `M410` abort.
- Controller state machine.
- Soft limits enforced in software (mandatory because endstops are wired-OR).
- REST endpoints for jog / move / home / stop / diagnostics; WebSocket telemetry.
- Web UI: per-axis jog panel, status badge, log tail, big red Abort button.
- Default YAML configs for all 8 axes + sample procedures.

Phase 2 (procedures DSL runner, mode switching, live fluidics SVG) and Phase 3 (pressure + step-skip test) are scaffolded — see `docs/ROADMAP.md`.

## Layout

```
flowengine/      # Python package: transport, hardware, api, schemas, state, ...
web/             # Templates + vanilla-JS frontend + CSS
config/          # YAML defaults (device_map, runtime, modes, procedures, profiles)
docs/            # Architecture, hardware bring-up, protocol, DSL reference
tests/           # Unit + integration tests (run against MockTransport)
scripts/         # run.sh, run_mock.sh, lint.sh, format.sh
```

User-specific overlays go to `$XDG_CONFIG_HOME/flowengine/` (e.g. `~/.config/flowengine/device_map.yaml`) and override repo defaults — keep them out of git.

## License

MIT.
