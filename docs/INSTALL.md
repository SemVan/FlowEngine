# Installation

## Prerequisites

- Python 3.11+ (check: `python3 --version`)
- For real hardware: an MKS Monster8 MK2 flashed with Marlin 2.x, connected over USB.
- For mock mode: nothing else.

## 1. Get the code and create a virtualenv

```bash
git clone https://github.com/SemVan/FlowEngine.git
cd FlowEngine

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

`-e` installs in editable mode so your source edits take effect immediately. `[dev]` pulls test/lint tooling.

## 2. Verify the install (mock mode)

```bash
pytest                              # all tests should pass
./scripts/run_mock.sh               # http://127.0.0.1:8765/
```

If the tests pass and the browser reports `connected_idle`, the software stack is working.

On Windows, where the shell scripts are not directly available, use:

```powershell
.venv\Scripts\activate
python -m flowengine --mock
```

## 3. USB / serial permissions for real hardware

### Linux

The Monster8 enumerates as `/dev/ttyACM0` (Marlin built with native USB) or `/dev/ttyUSB0` (via a serial adapter). Your user needs to be in the `dialout` group:

```bash
sudo usermod -aG dialout $USER
# log out and log back in
```

Check: `groups` should list `dialout`. Then:

```bash
ls -l /dev/ttyACM*
```

You should see the device. FlowEngine normally discovers it automatically. If
several compatible serial devices are connected, set `FLOWENGINE_SERIAL_PORT`
to the required path before starting the app.

If `ModemManager` is grabbing the port (you see it flicker open/close), block it for your VID/PID via udev rules — see Marlin/3D-printer wikis.

### macOS

Devices normally appear as `/dev/cu.usbmodem*` and are discovered automatically.
If necessary, run with an explicit path:

```bash
FLOWENGINE_SERIAL_PORT=/dev/cu.usbmodemXXXX ./scripts/run.sh
```

Some Monster8 revisions do not negotiate power correctly with a direct USB-C to
USB-C cable. A USB-A data cable through a USB-C adapter is a known working option.

For FTDI-based boards: the official driver and Apple's built-in CDC can conflict — uninstall the official driver and rely on Apple's.

### Windows

Marlin typically appears as `COMn` and should be discovered automatically. If
needed, find the number in Device Manager → Ports and run:

```powershell
$env:FLOWENGINE_SERIAL_PORT="COM3"
python -m flowengine
```

## 4. Run

```bash
./scripts/run.sh
```

Open `http://127.0.0.1:8765/`. A successful first connection shows
`connected_idle` and `diagnostics only`. Check the following buttons:

- **Read firmware** — Marlin identity and capabilities;
- **Read position** — logical axes and coordinates;
- **Read endstops** — current switch states;
- **Read settings** — steps, speed limits, currents, and other EEPROM settings;
- **Read drivers** — communication status of the stepper drivers.

Jog and Home must remain disabled while `motion.enabled` is `false`. Do not
enable motion until motors, drivers, power, directions, travel, and endstops have
been verified physically.

## 5. User overlays (optional)

Repo `config/*.yaml` are read-only defaults. To customize without polluting git, drop overrides into your XDG config dir:

```bash
mkdir -p ~/.config/flowengine
cp config/device_map.yaml ~/.config/flowengine/device_map.yaml
# edit the user copy; loader prefers it over the repo default
```

Audit log is written to `~/.local/state/flowengine/audit.jsonl`. Keep it; it's invaluable when something went wrong on the bench last week.

## Troubleshooting

- **`PermissionError: /dev/ttyACM0`** — you're not in `dialout`. Re-read step 3.
- **`Marlin USB serial port not found`** — check the cable, adapter, board power,
  and whether the device appears in the operating system.
- **`multiple Marlin serial ports found`** — select one with
  `FLOWENGINE_SERIAL_PORT`.
- **`Could not open serial port`** — another process owns it. On Linux, that's often ModemManager; on macOS, an old `pronterface`/`octoprint` session.
- **Browser shows "disconnected"** — server didn't start, or WebSocket can't reach it. Check `./scripts/run_mock.sh` output for tracebacks.
- **`All LOW` in Read drivers** — driver communication is not available. Common
  causes are absent drivers, missing motor power, or a mismatched UART/SPI configuration.

The tested firmware reports `EMERGENCY_PARSER:0`. Do not rely on the UI's
`M410` button as a guaranteed immediate physical stop; provide a hardware power cutoff.
