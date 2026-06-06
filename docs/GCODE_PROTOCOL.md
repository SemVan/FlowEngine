# G-code protocol

## Framing — line numbers + checksum

Every command we send to Marlin is framed as:

```
N<n> <payload>*<cs>\n
```

- `<n>` is a monotonically-increasing line number. We reset to 0 at connect via `M110 N0`.
- `<cs>` is Marlin's XOR checksum: XOR every byte of `N<n> <payload>` (up to but not including `*`).

Marlin will accept un-numbered lines too, but checksum framing is the only way to make USB-CDC dropouts *observable*. Without it, a corrupted command gets silently dropped and a syringe pump silently drifts.

## Resend

If Marlin can't validate a line, it replies with:

```
Error:checksum mismatch, Last Line: N
Resend: N+1
```

We respond by re-emitting the saved payload framed with the requested line number. We keep the last 64 sent lines around for resend; older than that, we error out.

`MarlinTransport.resend(N)` implements this.

## Commands we send

| Code | Meaning | When |
|---|---|---|
| `M110 N0` | Reset line counter | Once at connect |
| `M115` | Firmware capabilities | At connect; cached in `AppContext` |
| `M119` | Endstop snapshot | On `/api/diagnostics/endstops` |
| `M114` | Report current position | After `M400` whenever we want settled positions |
| `M400` | Wait for moves to finish | Before every `M114` that matters |
| `M410` | Quickstop (abort) | On `/api/stop` |
| `M92 <axis><steps_per_unit>` | Steps/unit per axis | On `sender.configure()` at startup |
| `M203 <axis><units/s>` | Max feedrate per axis | On `sender.configure()` |
| `G90` | Absolute positioning | On `sender.configure()` |
| `G28 <axes>` | Home | On `/api/home` (or `home` step) |
| `G1 <axis><target> F<feedrate>` | Linear move | On jog / move / procedure |
| `G92 <axis>0` | Set position | Crash-homing strategy (Phase ≥2) |

We do **not** send `M112` (kill — requires reboot). That's reserved for a future hardware E-stop button.

## Responses we parse

`flowengine/transport/parser.py` classifies every line into one of these:

| Kind | Example | Meaning |
|---|---|---|
| `ok` | `ok B15` | Command accepted; planner buffer hint optional |
| `resend` | `Resend: 42` | Re-emit line N42 |
| `error` | `Error:checksum mismatch, Last Line: 41` | Resend will follow (or hard error) |
| `busy` | `echo:busy: processing` | Marlin still chewing; restart the deadline |
| `echo` | `echo:M203 X1000` | Informational; keep waiting |
| `position` | `X:10.0 Y:0.0 Z:0.0 E:0.0` | M114 reply |
| `endstops` | `x_min: TRIGGERED` | M119 reply (one line per axis) |
| `firmware` | `FIRMWARE_NAME:Marlin 2.1.2 ... Cap:CHECKSUM:1` | M115 reply |
| `temperature` | `T:25.0 /0.0` | M105 reply — we ignore for now |
| `kill` | `!!` | Marlin halted; reset required (`TransportProtocolError`) |
| `other` | anything else | Logged at WARNING, dropped, **never** treated as ok |

## The `ok` discipline

CommandQueue (`flowengine/hardware/queue.py`) sends one line, then waits in a loop reading responses until it gets `ok`. While waiting, it:

- updates `result.position` if it sees an M114 reply,
- updates `result.endstops` from accumulated M119 lines,
- updates `result.firmware_caps` from M115,
- triggers `MarlinTransport.resend()` on `Resend: N` (and keeps waiting),
- restarts the deadline on `busy`,
- raises `ControllerError` on a real `Error:`,
- raises `TransportProtocolError` on `!!`,
- raises `TransportTimeout` if no `ok` arrives within the per-class deadline.

**Unknown lines are never silently consumed as `ok`.** They're logged at WARNING and discarded; the queue keeps waiting.

## Per-command timeouts

| Class | Default | Source |
|---|---|---|
| `ok_default` | 5 s | `runtime.yaml: timeouts.ok_default` |
| `move` | 30 s | `timeouts.move` |
| `homing` | 60 s | `timeouts.homing` |
| `diagnostics` | 3 s | `timeouts.diagnostics` |

Raise homing if your stroke is long and slow; lower diagnostics if you want faster failure on a dead controller.

## Things we deliberately do not do

- **Pipelining.** We send one line, await `ok`, send next. This is dramatically simpler and the latency cost for fluidics-class motion is invisible.
- **Marlin's "ADVANCED_OK" / `M155` host-keep-alive style.** Not needed without pipelining.
- **Anything that ties a command to a planner-buffer slot** (`B<n>` slot count is ignored in our handshake).
