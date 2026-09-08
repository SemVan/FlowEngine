# Procedures DSL

Procedures are YAML files. Shipped examples live in `config/procedures/*.yaml`; procedures saved in the web UI live in the user's FlowEngine config directory and shadow shipped files with the same name. The runner walks `steps` top-to-bottom. There are typed run parameters, but no loops or arbitrary expressions.

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

`name`, `version`, and `steps` are required. `description` is recommended but optional. Set `draft: true` while commissioning a procedure. A draft cannot be run end-to-end, but its steps can be previewed and deliberately run one at a time from the developer workbench.

## Developer workbench

Start in mock mode and open `http://127.0.0.1:8765/procedures`:

```bash
./scripts/run_mock.sh
```

The page supports the commissioning workflow:

1. Open a supplied draft or create a new one.
2. Add, remove, and reorder common steps with the visual builder.
3. Use the advanced JSON editor for parameter definitions or less common operations.
4. Save to validate the complete document.
5. Enter run parameters and use **Save & preview** to see the resolved steps without controller commands.
6. Use **Run only this step** to commission one selected step. On real hardware this still requires the global motion interlock, homing where applicable, and an explicit browser confirmation.
7. Clear `draft` only after the individual steps and their ordering have been checked. Only then can **Run complete procedure** start the full sequence.

The four supplied fluidics procedures are intentionally incomplete examples. Their real device names, signed pump travel, speeds, and valve calibration must be filled during hardware commissioning.

## Run parameters

Declare parameters at the top level and reference them as an entire value using `${name}`:

```yaml
draft: true
parameters:
  pump_axis:
    type: string
    default: X
  travel:
    type: number
    default: null
    minimum: -100.0
    maximum: 100.0
steps:
  - op: move
    axis: "${pump_axis}"
    by: "${travel}"
```

Supported parameter types are `number`, `integer`, `string`, and `boolean`. A missing `default` makes the parameter required at preview/run time. Numeric parameters may have `minimum` and `maximum`. References are substitutions, not string templates: `"${travel}"` is valid, while `"move_${travel}"` is not evaluated.

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

### `move_multi` *(accepted as a draft, execution not implemented yet)*

```yaml
- op: move_multi
  axes: { X: 10.0, Y: 20.0 }
  feedrate: 1200.0
  relative: false
```

This reserves the format for coordinated motion. It can be stored and previewed, but the runner currently refuses to execute it.

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

Reserved operation. It can be stored and previewed, but the runner currently refuses to execute it.

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

Anchor visible in telemetry and logs. It doesn't affect motion.

### `call`

Reuse another procedure as one logical step:

```yaml
- op: call
  procedure: wash_pump
  parameters:
    travel: 25.0
    feedrate: "${wash_feedrate}"
```

Child parameters may be constants or references to parameters of the parent procedure. Before preview or execution, FlowEngine recursively resolves every call into primitive steps. Preview returns both the flat step list and `step_paths`, for example `analysis step 4 → wash_pump step 2`.

Circular calls such as `A → B → A` are rejected, and nesting is limited to eight procedures. A complete run also rejects a draft anywhere in the call tree. During commissioning, **Run only this step** on a `call` deliberately runs the referenced procedure as one nested block, including a draft child, after confirmation.

The shipped `composite_wash_example` demonstrates a parent procedure passing its parameters into the reusable `wash_user` draft.

## Lint

Loading is two-step:

1. **Parse:** YAML → pydantic discriminated union on `op`. Unknown ops, missing fields, or wrong types fail here.
2. **Lint:** check that referenced axes / valves / pumps exist in the active `device_map.yaml`.

The Procedures page lists shipped procedures and saved user procedures. Files that fail schema validation are shown with the reason. Controller-dependent checks still occur when a step is executed: for example an unknown valve, an unhomed axis, or a soft-limit violation is refused.

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
- Keep reusable operations such as pump washing in a separate procedure and compose them with `call`; do not copy their primitive steps into every parent.
