# FIPunlocked

![status](https://img.shields.io/badge/status-working-brightgreen)
![platform](https://img.shields.io/badge/platform-Windows-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![SPAD](https://img.shields.io/badge/SPAD.neXt-not_required-success)
![licence](https://img.shields.io/badge/Logitech_driver-only_official_stack-blue)

**Turn a dusty Saitek/Logitech Flight Instrument Panel into a generic 320×240
display with 6 working buttons and 2 rotary knobs — driven over HTTP.**

![FIP device](docs/hero.png)

The Flight Instrument Panel (FIP, `VID_06A3 PID_A2AE`) was designed for flight
sims only. This project unlocks it as a **mini second screen + Stream-Deck-style
controller** for anything: system dashboard, news reader, custom pages, or plain
`POST /screen` with any image.

## ✨ Features

- 🖥️ **320×240 display** — push any PNG/JPG/BMP/WEBP, a URL, or a video file
- 🔘 **6 side buttons + 2 knobs** — live feedback, edge detection (press/release)
- 💡 **8 LEDs** — one per side button (lights on press, like a Stream Deck) plus
  two extra LEDs, all individually controllable
- 🧩 **FIP-native apps** (`fipui.py`) — each page is an app combining rendering,
  button/knob events, persistent LEDs and visual alerts
- 🎮 **REFLEX mini-game** — whack-a-mole across the 6 rows with LED mirroring,
  speed bonus, lives, level control on both knobs
- 📑 **5 built-in pages** (left knob cycles) — see below
- ⌨️ **Opt-in keyboard bindings** — map `ctrl+shift+s`, `volumemute`, `win+d`…
  on any control. *Default: controls act only on the FIP's own pages, nothing
  runs on the host.*
- 🌐 **HTTP API + SSE** on `:8199` — trivially scriptable from any language
- 🚫 **No SPAD.neXt, no licence, no subscription** — only the free official
  Logitech driver stack

## 🚀 Quickstart

```bash
# once, as admin: the official Logitech service (auto-start is set during install)
sc start DirectOutputService

pip install pillow psutil opencv-python   # opencv only for /video
cd fipd
python fipd3.py                            # -> http://127.0.0.1:8199
```

## 📑 The seven built-in pages

![FIP pages](docs/pages.png)

| Page | Content |
|---|---|
| **1 · Clock** | Big clock + date |
| **2 · System** | CPU / RAM / disk bars, live |
| **3 · Network** | Traffic counters + interface IPs |
| **4 · Button tester** | S1–S6 light up on press, knobs & select states — instant feedback |
| **5 · Reader** | Long text scrolled by the **right knob** (44 px/notch) with progress bar |
| **6 · fipui demo** | Scrollable menu (8 items, 6 visible × 40 px aligned with the buttons): right knob scrolls, S1–S6 and LEDs act on the **visible** rows, action items fire alerts |
| **7 · REFLEX** | Mini whack-a-mole game: moles pop in the 6 rows (LED mirrors), S1–S6 to whack — any button starts/replays, knobs adjust the level (the left knob is captured by the game on this page only — the two middle buttons keep paginating) |

Navigation is **bounded** (no wrap-around) and works from BOTH the left knob and
the two page buttons between the knobs (firmware-driven, via a `PageChange`
callback — with our 5 pages registered they cycle *our* pages instead of the
stock Saitek screens). The reader scrolls only when the right knob has no
binding configured.

## ⌨️ Keyboard bindings (Stream-Deck mode, opt-in)

Edit `fipd/fipd_bindings.json` (hot-reloaded) or:

```bash
curl -X POST -d '{"paging": true,
  "bindings": {"S1": "ctrl+shift+s", "S2": "volumemute"},
  "pages": {"1": {"S3": "f5"}}}' http://127.0.0.1:8199/bindings
```

- Controls: `S1..S6`, `select`, `left_cw/left_ccw`, `right_cw/right_ccw`
- 102 key names (`GET /keys`): letters, F-keys, modifiers, nav, media keys…
- Hold-aware: keys go down on press, up on release; knobs pulse per notch
- `pages` overrides per FIP page · `"paging": false` frees the left knob
- Native `SendInput` (scancodes) — no extra dependency

## 🔌 HTTP API

| Endpoint | Effect |
|---|---|
| `POST /screen` (body = image, or `?url=...`) | Display an image |
| `POST /video` (body = video file) | Play it (S1 = stop) |
| `POST /ui` | Back to the built-in pages |
| `GET /buttons` | SSE stream of button/knob events |
| `GET /events` | Last 50 events (JSON) |
| `GET/POST /bindings` | Read / update keyboard bindings |
| `POST /led` `{"index":1..8,"on":true}` | Control a LED (`{"scan":true}` to cycle) |
| `GET /keys` | Recognized key names |
| `GET /` | Status page |

## 🔬 How it works (the fun part)

The stock Logitech stack is: **`DirectOutput.dll`** — a named-pipes *client*
library apps link against — talking to **`DirectOutputService.exe`**, a SYSTEM
service hosting `DirectOutputSaiFip.dll` (the real device plugin).
`DirectOutput_Initialize` **blocks forever** when the service isn't running:
that's the trap everyone falls into. `fipd3.py` drives that stack from Python:

```
Initialize → Enumerate → RegisterSoftButtonCallback → AddPage(0, FLAG_SET_AS_ACTIVE)
→ SetImage (+ keepalive every 2 s)
```

Firmware quirks discovered along the way: HID reports only flow while a page is
**active** and images keep coming (the panel falls back to its home screen after
~2–3 s idle); you can only draw on the page the firmware **currently displays**
(`AddPage` makes the new page current, re-activating requires `RemovePage` +
`AddPage` — see `fipd/probe_pages2.py`); and a clean service shutdown closes the
session (USB power-cycle resets it).

Everything was reverse-engineered from scratch — disassembly of the plugin DLL
(capstone/pefile, `disas_*.py`), IL dumps of the client libraries (Mono.Cecil,
`cecil/`), Frida captures, and a **proxy DLL** (`proxy/`) swapped in front of the
real plugin inside the service to log its exact init sequence. The full story
lives in [`fipd/README.md`](fipd/README.md) (French); reference wire captures in
`fipd/startserver_session.log` and `fipd/proxy_log_copie.txt`.

## 🧩 Write your own FIP app

Pages are instances of `fipui.App` (see [`fipd/fipui.py`](fipd/fipui.py) — the
built-ins and the demo app in `fipd/fipd3.py` are living examples):

```python
class MyApp(fipui.App):
    name = 'myapp'
    title = 'MY APP'
    def on_enter(self):            # page becomes current
        self.set_led(1, True)      # persistent LED (re-applied on page switches)
    def on_event(self, name, pressed):
        if pressed and name == 'S1':
            self.alert('Hello!')   # banner + LED 8 blink, 3 s
    def render(self, d):           # 320x240, background already painted
        fipui.draw_header(d, F_MED, self.title)
        fipui.draw_menu(d, F_MED, self.items, self.cursor, self.states)
```

The daemon handles everything hardware: current-page rule, single display
thread (no RPC re-entrancy), keepalive, paging, keyboard bindings (an app
yields a control when it's bound). Apps can also `capture_left_knob = True`
to own the left knob on their page (like the REFLEX game). `GET /apps` lists pages, `POST /page
{"page":N}` switches.

## 💡 LEDs

`DirectOutput_SetLed` follows the same **current-page** rule as `SetImage`.
Valid indices: **1–6** (side buttons S1–S6) and **7–8** (two extra LEDs, on by
default on stock firmware — the daemon turns them off so you own them). LED
states are per firmware page. Button-press lighting can be disabled with
`"led_feedback": false` in `fipd_bindings.json`.

## 🗺️ Roadmap

- [x] **FIP-native app framework** (`fipui`) — shipped Nov 2026: apps combine
      display, buttons, knobs, persistent LEDs and alerts; 7 built-in pages
      including a scrollable settings menu and the REFLEX mini-game
- [ ] FIP apps: load third-party apps from an `apps/` directory (hot-load),
      more widgets (sliders, spinners, charts)
- [ ] Capture the exact wire bytes of the page-activation sequence → pure-Python
      `firmware_init()` (drop the Logitech stack entirely)
- [ ] **Linux port** — libusb for the bulk interface (44-byte big-endian header
      protocol, already decoded in `fip.py`) + hidraw for buttons

## 📁 Layout

- [`fipd/`](fipd) — the HTTP daemon (`fipd3.py`) + pure-Python protocol driver
  (`fip.py`) + test clients, Frida captures, `gen_screens.py` (doc screenshots)
- [`proxy/`](proxy) — gcc source of the x64 proxy DLL + deploy/restore script
- [`disas_*.py`](.) — targeted disassembly tooling
- [`cecil/`](cecil) — Mono.Cecil IL dump scripts
- [`docs/`](docs) — screenshots (exact renders of what the daemon sends)

## ⚠️ Notes

- Windows only for now (Linux port on the roadmap)
- `SendInput` bindings don't reach elevated windows unless the daemon runs elevated
- Button masks (HID `mi_00`, hardware-confirmed): `S1..S6 = 0x20..0x400`,
  right knob CW/CCW `0x02/0x04`, left knob CW/CCW `0x10/0x08`. Note: the FIP has
  **no physical select button** (mask 0x01 is never emitted; the `.dat`
  descriptor lists 12 controls, none of which is a select)
