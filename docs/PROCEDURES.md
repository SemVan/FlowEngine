# Procedures DSL

Procedures are YAML files. Shipped examples live in `config/procedures/*.yaml`; procedures saved in the web UI live in the user's FlowEngine config directory and shadow shipped files with the same name. The runner walks `steps` top-to-bottom. There are typed run parameters and a bounded repeat of a named child procedure, but no arbitrary expressions or branching.

> This is by design. Procedures remain **audit-readable** because `call` and `repeat` are expanded into a flat preview before execution. More complex control flow belongs outside this DSL.

## File format

`require_reference` checks existing session references without sending motion or
inventing a zero, for example `{op: require_reference, axes: [X, Y, U]}`.
`reference_pumps` uses it until the engineer configures verified automatic HOME.
`set_valve` accepts optional `feedrate` and `acceleration` for working-position moves.
`pump_multi` splits different nominal finish times into coordinated segments;
acceleration affects actual timing, so this is not independent motion planning.
Pump `allowed_routes` can require named valve positions for each pump direction.
See [the commissioning guide](COLLEAGUE_COMMISSIONING.md) for persistent evidence,
profile application and configuration transfer.

For `home_valve`, `search_distance` limits travel toward the switch;
`release_distance` separately limits automatic travel away until the switch opens.
`pull_off` adds clearance after the switch opens. `release_distance + pull_off`
must not exceed 20 axis units (the commissioning limit); the configured value
must fit the real mechanism, even when the configured axis travel is provisional.
If `release_distance` is omitted, the legacy search/travel limit is retained.
The same release limit applies when starting with an already pressed shared input:
verify that the selected valve is the one pressing it. A failed search/release
leaves that valve unreferenced and releases its motor.

```yaml
name: prime_sample
description: "Prime the sample line"
version: 1
steps:
  - op: home
    axes: [X]
  - op: set_valve
    name: sample_valve
    position: A
  - op: move
    axis: X
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

The supplied `startup`, `wash`, `sample_intake`, `analysis_delivery`, `measurement_iteration`, and `cyclic_measurement` procedures are drafts reconstructed from the provided workbook. Their positions, volumes, flows, accelerations and unverified B/C home directions must be commissioned before full execution.

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

Additional executable operations (all examples remain drafts until commissioned):

| Operation | Required fields | Meaning |
|---|---|---|
| `pump` | name, volume_ul, flow_ul_min | Calibrated fluid movement; direction dispense/aspirate |
| `pump_multi` | pumps: mapping from pump name to volume_ul / flow_ul_min / direction | ≥2 calibrated pumps; matching nominal durations |
| `motors` | enabled | Optional axes; release invalidates their home |
| `read_sensors` | — | One M105 report mapped through analog_inputs |
| `seek` | axis, channel, by, feedrate | Verified G38.2 contact search; optional zero |
| `home_valve` | name, channel, direction, search_distance | Host-stepped home of one valve against its single switch on a shared input |
| `calibrate_valve` | axis, channel, search_distance, feedrate, backoff | Legacy two-contact span measurement; not applicable to the current one-switch valves |
| `test_reference` | axis, channel, by, feedrate, reference_feedrate, backoff, tolerance_pulses | Return-reference error; repeats=3 |
| `repeat` | procedure, times | Repeat one named child procedure 1–1000 times; optional child parameters |

`move` also accepts units=axis/steps, speed_units=axis/min, axis/s or steps/s.
`move`, `move_multi` and `pump` accept acceleration in axis units/s²; it is scoped
to the movement and restored from the M503 M204 P/R/T report.
Preview exposes static execution_issues; full execution checks unsupported operations
and references before starting. Travel and current position are still checked at execution.

`seek`/calibration operations are disabled unless firmware.probe_target_verified,
verified_probe_axes and probe_channel describe an actually bench-verified mapping.
These config fields do not modify firmware pins. Home-direction convention also applies
to zero=true. After a raw command, motor release, Stop or uncertain motion, positions
may need re-homing. Stop leaves the queue faulted until application restart.

Practical examples are shipped in `config/procedures/` as drafts.
Hardware caveats: [COMMISSIONING.md](COMMISSIONING.md).

### `move`

```yaml
- op: move
  axis: X           # Marlin axis letter (see device_map.yaml)
  to: 25.0          # absolute target (one of `to` / `by`, not both)
  by: 5.0           # OR relative move
  feedrate: 400.0   # optional; defaults to axis.feedrate_default
```

Soft limits are enforced before the G-code is sent.

### `move_multi` (coordinated movement)

```yaml
- op: move_multi
  axes: { X: 10.0, Y: 20.0 }
  feedrate: 1200.0
  relative: false
```

Executes one coordinated G1. Choose feedrate OR duration_s (nominal constant-speed duration,
excluding acceleration/deceleration). Axes finish together; this is not independent planners.
Matching axis units, homed axes, component speed limits and non-shared endstop inputs are required.

### `home`

```yaml
- op: home
  axes: [E0]        # omit `axes` to home all
```

Do not use `home`/G28 for `A/B/C`: their three individual switches share `z_min`.
Use `home_valve` sequentially so only one mechanism can cause the shared signal:

```yaml
- op: home_valve
  name: wash_valve
  channel: z_min
  direction: -1
  step: 0.25
  feedrate: 30
  search_distance: 9
  pull_off: 0.5
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
  position: "КС-ЗАБОР" # A/B or a name from valve.position_names
```

Semantic wrapper around `move` for two-position valves.
Execution is refused while that valve has `calibrated: false`; preview still shows the resolved target.

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

### `repeat`

```yaml
- op: repeat
  procedure: measurement_iteration
  times: "${cycles}"
  parameters:
    sample_volume_ul: "${sample_volume_ul}"
```

`times` must resolve to an integer from 1 to 1000. Preview expands every iteration and labels paths as `repeat 1/N`, `repeat 2/N`, and so on. Recursive calls and repeats are rejected.

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

- One file per reusable procedure. Compose larger workflows with `call` and bounded `repeat`.
- Keep names snake_case. They become URL segments (`/api/procedures/<name>/run`).
- Prefer absolute `to:` over relative `by:` when the move ends at a known position (e.g., parking the autosampler). Use `by:` only for relative-feed operations like priming.
- `log` steps at start and end of long procedures — the audit log will thank you when something goes wrong at 3 AM.
- Don't over-comment the YAML. Names should carry the meaning.

## Anti-patterns

- "Just one little `if`." — Write two procedures, let the operator choose.
- "Loop this 5 times." — Five lines. Verbose is auditable.
- "Embedded math (`feedrate: 100 * 2`)." — Compute once, store the result.
- Keep reusable operations such as pump washing in a separate procedure and compose them with `call`; do not copy their primitive steps into every parent.
