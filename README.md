# FIPunlocked — custom driver for the Saitek/Logitech Flight Instrument Panel

Turn a Saitek/Logitech **Flight Instrument Panel** (FIP, `VID_06A3 PID_A2AE`) into a
generic **320×240 display** whose **6 buttons and 2 rotary knobs** are usable from
your own software — with an HTTP API.

- Working on **Windows** (validated Nov 19, 2026) — no SPAD.neXt, no licence needed.
- **Linux port** in progress (pure-protocol init, see *Roadmap*).

```bash
cd fipd
python fipd3.py        # -> http://127.0.0.1:8199
```

| Endpoint | Effect |
|---|---|
| `POST /screen` (body = PNG/JPG/BMP/WEBP, or `?url=...`) | Display an image |
| `POST /video` (body = video file) | Play a video (button S1 = stop) |
| `POST /ui` | Built-in UI (clock / system / network; left knob = page) |
| `GET /buttons` | Server-Sent Events for buttons & knobs |
| `GET /events` / `GET /` | Recent events / status page |

Button masks (HID `mi_00`, confirmed on hardware): `S1..S6 = 0x20..0x400`,
select `0x01`, right knob CW/CCW `0x02/0x04`, left knob CW/CCW `0x10/0x08`.

## How it works (the interesting part)

The stock Logitech stack is: **`DirectOutput.dll`** (a named-pipes *client* library
apps link against) talking to **`DirectOutputService.exe`** (a SYSTEM service which
hosts `DirectOutputSaiFip.dll`, the actual device plugin). `DirectOutput_Initialize`
blocks forever when the service is not running — that's the trap everyone falls into.

`fipd3.py` uses that official stack from Python via ctypes:

```
Initialize → Enumerate → RegisterSoftButtonCallback → AddPage(0, FLAG_SET_AS_ACTIVE)
→ SetImage (+ keepalive every 2 s)
```

The firmware only emits HID reports while a page is **active** and images keep
flowing (it falls back to its Saitek home screen after ~2–3 s otherwise).

Everything was reverse-engineered from scratch: disassembly of the plugin DLL
(capstone/pefile scripts at repo root), IL dumps of the client libraries
(Mono.Cecil, `cecil/`), Frida captures of live sessions, and a **proxy DLL**
(`proxy/`) swapped in front of the real plugin inside the service to log its exact
call sequence. The full story and findings live in [`fipd/README.md`](fipd/README.md)
(French). Reference captures: `fipd/startserver_session.log` (wire sequence of the
plugin's `Create`) and `fipd/proxy_log_copie.txt` (the service's init calls).

## Layout

- [`fipd/`](fipd) — the HTTP daemon (`fipd3.py`, current) + pure-Python protocol
  driver (`fip.py`) + historical backends, test clients and Frida capture scripts
- [`proxy/`](proxy) — gcc source of the x64 proxy DLL + elevated deploy/restore script
- [`disas_*.py`](.) — targeted disassembly tooling for `DirectOutputSaiFip.dll`
- [`cecil/`](cecil) — Mono.Cecil IL dump scripts

## Roadmap

1. Capture the exact wire bytes of the `AddPage(…, ACTIVE)` equivalent (enhanced
   proxy hooking the plugin's `WriteFile`) → pure-Python `firmware_init()`.
2. Linux port: `MI_01` bulk interface via libusb (44-byte big-endian header
   protocol, already decoded in `fip.py`), buttons via `/dev/hidraw`.

## Requirements (Windows)

- Logitech DirectOutput driver package installed
  (`C:\Program Files\Logitech\DirectOutput`), service `DirectOutputService` running
  (`sc start DirectOutputService` as admin; on the dev machine it is set to
  `start= auto`)
- Python 3.10+ with `pillow`, `psutil` (`opencv-python` for `/video`)

## Keyboard shortcuts (Stream-Deck style)

Each button/knob can fire a keyboard combo (native `SendInput`, no extra
dependency). Edit `fipd/fipd_bindings.json` (hot-reloaded) or use the API:

```bash
curl -X POST -d '{"paging": true,
  "bindings": {"S1": "ctrl+shift+s", "S2": "volumemute", "right_cw": "pagedown"},
  "pages": {"1": {"S3": "f5"}}}' http://127.0.0.1:8199/bindings
```

- Buttons `S1..S6`, `select`, knobs `left_cw/left_ccw/right_cw/right_ccw`
- Combos like `ctrl+shift+s`, `win+d`, `alt+tab`; 102 key names via `GET /keys`
  (media keys included: `volumemute`, `playpause`, `medianext`…)
- `pages` overrides bindings per FIP page (ui mode); `"paging": false` frees the
  left knob (default: left knob keeps switching the built-in UI pages)
- Hold-aware: keys go down on press, up on release
