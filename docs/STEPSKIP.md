# Step-skipping test — protocol design (Phase 3)

This document describes the planned protocol. The implementation is scaffolded but not delivered in Phase 1; see `flowengine/experiments/stepskip.py`.

## Why

Steppers under high back-pressure (a clogged or near-full syringe) can lose steps silently. The motor moves, the controller thinks it moved further than it did, and your dispensed volume is wrong. Endstops won't catch this; you need a positive-control test on the bench to characterize the safe operating envelope.

The flow cytometer's syringe pumps are the highest-risk: a single missed batch of steps over a 1 mL injection changes the measured sample concentration. Knowing the feedrate at which loss begins, at expected back-pressures, lets us cap `feedrate_max` safely.

## What the test does

For a single axis (typically a syringe pump), the test runs a parameterized ramp:

```
for each setpoint in ramp:
    record baseline position (M400; M114)
    command N steps at the current setpoint
    wait_idle (M400; M114)
    record settled position
    sample pressure (PressureSensor.read())
    delta = (reported - commanded)
    if abs(delta) > tolerance:
        mark setpoint as "step-loss"
        optionally: stop, or continue to characterize the curve
```

Setpoint dimension is either **feedrate** (most common) or **load** (back-pressure target, via a downstream valve restriction). For the syringe-pump first pass we ramp feedrate at a fixed back-pressure (open valve or pinch clamp).

## Parameters

| Parameter | Meaning | Typical value |
|---|---|---|
| `axis` | Marlin axis under test | `E0` / `E1` / `E2` |
| `setpoint_dim` | What we vary | `feedrate` |
| `setpoint_range` | [start, stop, step] | e.g. `[100, 1500, 100]` mm/min |
| `move_per_step` | Commanded distance per setpoint | e.g. 5 mm |
| `tolerance` | When to declare loss | e.g. 0.05 mm |
| `back_pressure` | Optional reference; manual entry if no sensor | — |
| `rest_between_s` | Pause between setpoints | 1 s |

## Output

1. **CSV** at `~/.local/state/flowengine/stepskip/<timestamp>.csv`:
   ```
   setpoint, commanded_mm, reported_mm, delta_mm, pressure, lost
   100, 5.000, 5.000, 0.000, 12.3, False
   200, 5.000, 4.998, -0.002, 13.1, False
   ...
   1300, 5.000, 4.870, -0.130, 27.4, True
   ```

2. **Markdown summary** with the first-loss setpoint and a chart of `delta_mm` vs `setpoint`.

3. **Suggested action**: a one-line recommendation, e.g.
   > Cap `feedrate_max` for E0 at **1100 mm/min** (one safety margin step below first-loss at 1300).

## What this test does *not* do

- It does not test long-term reliability (do a soak separately).
- It does not test acceleration limits in isolation (a separate experiment).
- It does not characterize cross-axis interactions (one axis at a time).

## When to run

- At commissioning, once mechanical assembly is final.
- After any mechanical change (new syringe, new tubing geometry, new motor coupling).
- If procedures start producing unexpected pressure traces.

## Source of pressure

Phase 1 ships `ManualSensor` and `MockSensor`. The real sensor source is TBD — see `docs/HARDWARE.md` → "Pressure sensor source" for the open decision. Until that's resolved, the test can still run with `ManualSensor` (operator types in a pressure reading per setpoint) — slow, but valid.
