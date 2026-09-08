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
git clone https://github.com/SemVan/FlowEngine.git
cd FlowEngine

# Recommended: a virtualenv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"

# Run the test suite (mock-driven)
pytest

# Launch the app against a simulated controller
./scripts/run_mock.sh
# → open http://127.0.0.1:8765/
```

Without real hardware you can exercise the UI, motion API, procedures, state
updates, diagnostics, and soft limits against a simulated Marlin controller.

## Quickstart (real hardware)

1. Connect the Marlin-flashed Monster8 over USB. For USB-C hosts, a USB-A data
   cable plus a USB-C adapter may be required by boards without correct USB-C CC wiring.
2. Leave `motion.enabled: false` and `motion.configure_firmware: false` in
   `config/runtime.yaml` for the first connection.
3. Run `./scripts/run.sh` and open `http://127.0.0.1:8765/`.
4. Use **Read firmware**, **Read position**, **Read endstops**, **Read settings**,
   and **Read drivers** in the web UI.

FlowEngine auto-detects the Marlin USB CDC port (including the STM32
`0483:5740` device used during bring-up). Override it only when auto-detection
is ambiguous:

```bash
FLOWENGINE_SERIAL_PORT=/dev/cu.usbmodemXXXX ./scripts/run.sh  # macOS
FLOWENGINE_SERIAL_PORT=/dev/ttyACM0 ./scripts/run.sh          # Linux
```

The shipped map reflects the prototype configuration recovered from
`cytonator3000`: `X` and `Y` are syringe pumps, `A/B/C` are valves, and `U` is
the wash pump. This mapping and all calibration values still require physical
verification. Do not enable motion before checking drivers, wiring, directions,
limits, and endstop polarity.

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

## Current status

- Marlin serial transport with optional checksum/line-number framing.
- Automatic Marlin USB-port discovery on macOS, Linux, and Windows.
- Mock transport with the same `ok` / error / disconnect semantics for CI.
- Single-writer command queue with response parsing and timeouts.
- Controller state machine.
- Motion interlock plus homing and software travel limits.
- REST endpoints for jog / move / home / stop / diagnostics; WebSocket telemetry.
- Read-only `M115`, `M114`, `M119`, `M503`, and `M122` diagnostics in the web UI.
- YAML procedure execution for homing, moves, valve positions, dwell, and logging.
- Developer procedure workbench: validated drafts, typed parameters, visual step builder,
  dry preview, and deliberate one-step-at-a-time commissioning.
- Saved configuration profiles and editable 8×12 autosampler rack geometry.
- A reconstructed `X/Y/A/B/C/U` device map and matching example procedures.
- Automated tests covering the parser, queue, limits, state, WebSocket, and mock stack.

Mode switching, live fluidics visualization, pressure feedback, variable loops,
and multi-controller support are not implemented yet. The Fluidics diagram is a
draft, not a verified tubing diagram.

> **Safety:** the current real-hardware profile starts in diagnostics-only mode.
> Also, the tested firmware reports `EMERGENCY_PARSER:0`, so `M410` must not be
> treated as a guaranteed physical emergency stop. Use a hardware power cutoff.

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
