# Architecture

## Layers

```
┌─────────────────────────────────────────────────────────────┐
│ Browser (vanilla JS modules)                                │
│   panels/jog, panels/status, panels/log, ...                │
│   store.js   ws.js   api.js                                 │
└──────────────────────────┬──────────────────────────────────┘
              HTTP (REST, idempotency keys) + WebSocket (telemetry)
                           │
┌──────────────────────────▼──────────────────────────────────┐
│ FastAPI app (flowengine.app)                                │
│   routers: motion / diagnostics / config / procedures /     │
│            modes / stepskip / ws                            │
│   AppContext: DI singleton                                  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│ Controller layer                                            │
│   StateMachine     (state.py)                               │
│   EventBus         (events.py)        ──► WS subscribers    │
│   GcodeSender      (hardware/sender.py)                     │
│   MotionModel      (hardware/motion.py, soft limits)        │
│   HomingStrategy   (hardware/homing/*.py)                   │
│   Valve, Pump      (hardware/valve.py, pump.py)             │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│ CommandQueue (hardware/queue.py)                            │
│   • single-line `ok` handshake                              │
│   • per-command timeouts                                    │
│   • classifies every response line                          │
│   • abort = M410 quickstop                                  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│ Transport (transport/)                                      │
│   MarlinTransport — line-number + checksum framing, resend  │
│   MockTransport   — mirrors Marlin semantics for CI/dev     │
│   KlipperTransport — stub (NotImplementedError with guide)  │
│   parser.py       — typed response classification           │
└─────────────────────────────────────────────────────────────┘
```

## State machine

```
              disconnected
                  ▲
                  │ open()
                  ▼
              connected_idle ──► moving ──► connected_idle
                  │  ▲             │
        home() ──►   │             └─► aborting ─► connected_idle
                  │  │
                  ▼  │  abort/error
              homing  errored ──► connected_idle (after operator reset)
                  │
                  └──► connected_idle
```

Every operation calls `state.require(...)` first. Illegal transitions raise `StateError` (HTTP 409). Disconnect kicks the controller back to `disconnected` and forces re-home on reconnect — Marlin loses position on USB reset, and silently re-using stale coordinates is a great way to break syringes.

## Command lifecycle

```
client REST request
  ├─► AppContext.sender.jog(axis, delta, fr)
  │     ├─► motion.plan_relative(...)  ──► SoftLimitError if out of bounds
  │     └─► queue.send("G1 X… F…", timeout=30s)
  │           ├─► transport.send_line(...) (adds N<n> ... *<cs>)
  │           └─► loop: transport.read_line()
  │                 ├─ Ok        → return
  │                 ├─ Resend N  → transport.resend(N), keep waiting
  │                 ├─ Error     → ControllerError
  │                 ├─ Busy/Echo → restart deadline, keep waiting
  │                 └─ Other     → log warning, keep waiting
  └─► JSON response with new positions
```

Notes:

- We **do not** pipeline. One command in flight at a time. Pipelining requires Marlin's full advance-buffer + resend protocol, which we don't implement. For fluidics-class motion the simplicity wins.
- `M400` precedes any `M114` we actually care about — without it, M114 reports the *commanded* position mid-motion, not the settled one.
- Abort sends `M410` (Marlin quickstop). It does *not* send `M112` (kill — requires reboot). `M112` is reserved for a dedicated hardware-style E-stop UI affordance.

## Extension points

### Adding a new device type

1. Add a `DeviceKind` value in `flowengine/schemas/device.py`.
2. If it needs a domain object (like `Valve`, `Pump`), add a new file under `flowengine/hardware/`.
3. Reference it in `config/device_map.yaml`.
4. If it adds a new procedure primitive, extend `flowengine/schemas/procedure.py`'s discriminated union.

### Adding a new transport

1. Subclass `Transport` in `flowengine/transport/`.
2. Mirror every relevant Marlin response variant if you want CI tests to apply.
3. Make `flowengine.app._build_transport` aware of it.

### Adding a new homing strategy

1. Subclass `HomingStrategy` under `flowengine/hardware/homing/`.
2. Wire it into `device_map.yaml: axes[*].homing_strategy`.
3. App startup currently constructs a single strategy from the runtime config; multi-strategy dispatch will arrive when the first axis actually needs it (don't pre-generalize).

### Adding a procedure primitive

1. Add a typed step model to `flowengine/schemas/procedure.py` (with a unique `op` literal).
2. Add a lint rule in `flowengine/procedures/loader.py:lint`.
3. Once the Phase-2 runner ships, dispatch the new step there.

## Concurrency model

- asyncio throughout. **No threads.**
- The transport owns its read loop in a single task. The send lock guarantees one outbound line at a time.
- WebSocket fanout is via `EventBus` (asyncio queues per subscriber). Slow subscribers drop their oldest queued messages — never block the producer.

## File layout (selected)

```
flowengine/
  app.py              # FastAPI factory + lifespan
  __main__.py         # `python -m flowengine`
  config.py           # CLI/env settings; XDG overlay resolver
  loaders.py          # YAML → pydantic
  state.py            # state machine
  events.py           # pub/sub
  errors.py           # typed exception hierarchy
  logging_setup.py    # structlog + JSONL audit
  schemas/            # pydantic models (single source of truth)
  transport/          # wire layer (Marlin / Mock / Klipper-stub)
  hardware/
    queue.py          # CommandQueue
    sender.py         # GcodeSender (jog/move/home/wait_idle/...)
    motion.py         # MotionModel (soft limits + units)
    homing/           # pluggable strategies
    pressure/         # pluggable sensors
    valve.py, pump.py # domain objects
  procedures/         # YAML loader + lint (P1), runner (P2)
  modes/              # P2
  experiments/        # P3
  api/                # one router per concern + ws.py
web/
  templates/          # Jinja2
  static/             # css + js (vanilla, ES6 modules)
config/               # YAML defaults
docs/                 # YOU ARE HERE
tests/                # pytest, hits FastAPI + MockTransport end-to-end
```
