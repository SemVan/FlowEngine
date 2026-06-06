# Installation

## Prerequisites

- Python 3.11+ (check: `python3 --version`)
- For real hardware: an MKS Monster8 MK2 flashed with Marlin 2.x, connected over USB.
- For mock mode: nothing else.

## 1. Get the code and create a virtualenv

```bash
git clone <this-repo> FlowEngine
cd FlowEngine

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -e ".[dev]"
```

`-e` installs in editable mode so your source edits take effect immediately. `[dev]` pulls test/lint tooling.

## 2. Verify the install (mock mode)

```bash
pytest                              # all tests should pass
./scripts/run_mock.sh               # http://127.0.0.1:8765/
```

If pytest is green and the browser shows a working jog panel without any USB device plugged in, you're good.

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

You should see the device. If not, run `dmesg | tail` after plugging in to find the actual device name and edit `config/runtime.yaml` (`transport.port`).

If `ModemManager` is grabbing the port (you see it flicker open/close), block it for your VID/PID via udev rules — see Marlin/3D-printer wikis.

### macOS

Devices appear as `/dev/tty.usbmodem*`. Set `FLOWENGINE_SERIAL_PORT=/dev/tty.usbmodemXYZ` in your `.env` (copy from `.env.example`). The first time you plug the board in, macOS may prompt to allow it under System Settings → Privacy & Security.

For FTDI-based boards: the official driver and Apple's built-in CDC can conflict — uninstall the official driver and rely on Apple's.

### Windows

Marlin typically appears as `COMn`. Find the COM number in Device Manager → Ports. Set `FLOWENGINE_SERIAL_PORT=COM3` (or whichever).

## 4. Run

```bash
cp .env.example .env                # then edit FLOWENGINE_SERIAL_PORT
./scripts/run.sh
```

Open `http://127.0.0.1:8765/`. The status badge will show `connected_idle` if everything is wired correctly. Read `docs/HARDWARE.md` next — do not jog anything blindly.

## 5. User overlays (optional)

Repo `config/*.yaml` are read-only defaults. To customize without polluting git, drop overrides into your XDG config dir:

```bash
mkdir -p ~/.config/flowengine
cp config/device_map.yaml ~/.config/flowengine/device_map.yaml
# edit the user copy; loader prefers it over the repo default
```

Audit log is written to `~/.local/state/flowengine/audit.jsonl`. Keep it; it's invaluable when something went wrong on the bench last week.

## 6. uPlot (Phase 3 — streaming pressure chart)

uPlot is vendored in `web/static/js/vendor/`. The Phase 3 pressure plot won't render until it's there. To install:

```bash
curl -L -o web/static/js/vendor/uplot.iife.min.js \
  https://unpkg.com/uplot@1.6.30/dist/uPlot.iife.min.js
curl -L -o web/static/js/vendor/uplot.min.css \
  https://unpkg.com/uplot@1.6.30/dist/uPlot.min.css
```

(These files are gitignored to keep the repo source-only.)

## Troubleshooting

- **`PermissionError: /dev/ttyACM0`** — you're not in `dialout`. Re-read step 3.
- **`Could not open serial port`** — another process owns it. On Linux, that's often ModemManager; on macOS, an old `pronterface`/`octoprint` session.
- **Browser shows "disconnected"** — server didn't start, or WebSocket can't reach it. Check `./scripts/run_mock.sh` output for tracebacks.
- **`firmware features missing: CHECKSUM`** — your Marlin build was compiled without `EMERGENCY_PARSER` / `LIN_ADVANCE` toggles; the relevant flag is `CHECKSUM`. Recompile Marlin with `Configuration_adv.h` defaults or remove the requirement from `runtime.yaml` (not recommended).
