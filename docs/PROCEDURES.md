# Procedures DSL

Procedures are YAML files in `config/procedures/*.yaml`. Each file is one procedure. The runner walks `steps` top-to-bottom; no loops, no conditionals beyond `wait_pressure` (Phase 3), no variables. If you need any of those, write a Python script that calls the REST API directly.

> This is by design. Procedures are **audit-readable** because they're flat data. Adding a `for` loop turns this into a half-baked programming language; if you need control flow, write Python.

## File format

```yaml
name: prime_sample
description: "Prime the sample line"
version: 1
steps:
  - op: home
    axes: [E0]
  - op: set_valve
    name: sample_valve
    position: A
  - op: move
    axis: E0
    by: 30.0
    feedrate: 300.0
  - op: dwell
    seconds: 2
  - op: log
    message: "primed"
```

`name`, `version`, and `steps` are required. `description` is recommended but optional.

## Primitive set

### `move`

```yaml
- op: move
  axis: E0          # Marlin axis letter (see device_map.yaml)
  to: 25.0          # absolute target (one of `to` / `by`, not both)
  by: 5.0           # OR relative move
  feedrate: 400.0   # optional; defaults to axis.feedrate_default
```

Soft limits are enforced before the G-code is sent.

### `move_multi`

```yaml
- op: move_multi
  axes: { X: 10.0, Y: 20.0 }
  feedrate: 1200.0
  relative: false
```

Coordinated multi-axis move on one G-code line. Phase 2.

### `home`

```yaml
- op: home
  axes: [E0]        # omit `axes` to home all
```

### `dwell`

```yaml
- op: dwell
  seconds: 2.5
```

Server-side timer (not Marlin's `G4`) — so abort wakes it up cleanly.

### `set_param`

```yaml
- op: set_param
  name: motion.feedrate_cap
  value: 1500
```

Update one runtime parameter for the duration of the procedure. Restored at end. Phase 2.

### `set_valve`

```yaml
- op: set_valve
  name: sample_valve   # must match a `valves[*].name` in device_map
  position: A          # or B
```

Semantic wrapper around `move` for two-position valves.

### `wait_pressure`

```yaml
- op: wait_pressure
  cmp: ">"             # one of <, >, between
  value: 50.0          # for < and >
  # for between:
  # min: 30.0
  # max: 60.0
  timeout: 10.0
```

Block until the configured pressure source crosses the threshold or the timeout fires. Phase 3.

### `log`

```yaml
- op: log
  message: "step complete"
```

Appended to the audit JSONL and broadcast on the WS log channel.

### `checkpoint`

```yaml
- op: checkpoint
  name: after_prime
```

Anchor for abort/resume diagnostics. Doesn't affect motion. Phase 2.

## Lint

Loading is two-step:

1. **Parse:** YAML → pydantic discriminated union on `op`. Unknown ops, missing fields, or wrong types fail here.
2. **Lint:** check that referenced axes / valves / pumps exist in the active `device_map.yaml`.

The Procedures page lists every file under `config/procedures/`. Files that fail parse or lint are shown red with the reason. Run-ability requires both to pass.

You can also lint from the CLI:

```bash
python -c "from flowengine.loaders import load_device_map, list_procedures; \
           from flowengine.procedures import load_and_lint; \
           dm = load_device_map(); \
           [load_and_lint(p, dm) for p in list_procedures()]"
```

## Style

- One file per procedure. Composability is "operator runs procedure A then procedure B," not include directives.
- Keep names snake_case. They become URL segments (`/api/procedures/<name>/run`).
- Prefer absolute `to:` over relative `by:` when the move ends at a known position (e.g., parking the autosampler). Use `by:` only for relative-feed operations like priming.
- `log` steps at start and end of long procedures — the audit log will thank you when something goes wrong at 3 AM.
- Don't over-comment the YAML. Names should carry the meaning.

## Anti-patterns

- "Just one little `if`." — Write two procedures, let the operator choose.
- "Loop this 5 times." — Five lines. Verbose is auditable.
- "Embedded math (`feedrate: 100 * 2`)." — Compute once, store the result.
- "Reference one procedure from another (`include: home_all`)." — Composition belongs in the UI, not the DSL.
