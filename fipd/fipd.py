"""
fipd v2 — Flight Instrument Panel : daemon generique (affichage + interface + video + boutons).

Initialisation firmware : lance SPAD.neXt (active le serveur embarque du FIP), le tue,
puis ouvre IMMEDIATEMENT un lecteur HID permanent (sinon le firmware coupe les events).
Affichage : DirectOutputSaiFip.dll via ctypes (fip_native).

API http://127.0.0.1:8199 :
  POST /screen (PNG/JPG)   affiche une image      POST /screen?url=...
  POST /video (fichier)    joue une video (bouton 1 = stop)
  POST /ui                 interface integree (horloge/systeme/reseau, knob gauche = page)
  POST /clear /led         GET /buttons (SSE) /events /
"""
import io
import json
import re
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ctypes
import psutil
from PIL import Image, ImageDraw, ImageFont

import fip
import fip_native
from fip_native import FipNative, SOFTBUTTONS

PORT = 8199
SPAD_EXE = r'C:\Program Files\SPAD.neXt\SPAD.neXt.exe'
SPAD_LOG = os.path.expandvars(r'%APPDATA%\SPAD.neXt\logs\spad.log')


_DLG_BTN = re.compile(r'^(ok|skip|dismiss.*)$', re.I)


def ensure_firmware_active(timeout=420, reader=None):
    """Lance SPAD, rejete ses dialogues via UIA invoke(), attend l'init du FIP, le tue.
    Sequence validee le 04/11/2026 (session fipd)."""
    if not os.path.exists(SPAD_EXE):
        return False
    pos = 0
    try:
        with open(SPAD_LOG, 'rb') as f:
            f.seek(0, 2)
            pos = f.tell()
    except OSError:
        pass
    subprocess.Popen([SPAD_EXE])
    t0 = time.time()
    ok = False
    app = None
    while time.time() - t0 < timeout:
        time.sleep(2)
        fresh = b''
        try:
            with open(SPAD_LOG, 'rb') as f:
                f.seek(pos)
                fresh = f.read()
        except OSError:
            pass
        # rotation du log SPAD : si le fichier a retreci, repartir du debut
        try:
            if os.path.getsize(SPAD_LOG) < pos:
                pos = 0
        except OSError:
            pass
        txt = fresh.decode('utf-8', 'replace')
        if 'Version fip' in txt or 'Turn device ON' in txt:
            ok = True
            if reader is not None and not reader.is_alive():
                reader.start()      # config v2.1 (succes 23h28) : lecteur AVANT le kill
            time.sleep(8)           # laisser SPAD finir sa sequence (pages, Turn ON)
            break
        # NOTE: aucun rejet de dialogues — le Warnings se ferme seul (~2 min)
        # (les invoke pywinauto tuaient SPAD via des boutons caches)
        time.sleep(1)
    subprocess.run(['powershell', '-NoProfile', '-Command',
                    'Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force'],
                   capture_output=True)
    return ok


class ButtonReader(threading.Thread):
    """Lecteur HID permanent : toujours une ReadFile pendante (jamais de trou)."""

    def __init__(self, on_event):
        super().__init__(daemon=True)
        self.on_event = on_event
        self._stop = threading.Event()

    def run(self):
        k32 = fip.kernel32
        paths = [p for p in fip.find_hid_paths() if 'mi_00' in p.lower()]
        if not paths:
            return
        h = k32.CreateFileW(paths[0], fip.GENERIC_READ, 3, None, 3, fip.FILE_FLAG_OVERLAPPED, None)
        got = fip.wt.DWORD(0)
        last = 0
        while not self._stop.is_set():
            buf = ctypes.create_string_buffer(8)
            ev = k32.CreateEventW(None, True, False, None)
            ov = fip.OVERLAPPED()
            ov.hEvent = ev
            k32.ReadFile(h, buf, 3, ctypes.byref(got), ctypes.byref(ov))
            r = k32.WaitForSingleObject(ev, 300)
            k32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(got), False)
            k32.CloseHandle(ev)
            if r == 0 and got.value >= 2:
                state = buf.raw[1] | (buf.raw[2] << 8)
                changed = state ^ last
                last = state
                if changed:
                    names = [n for b, n in SOFTBUTTONS.items() if changed & b]
                    try:
                        self.on_event(state, changed, names)
                    except Exception:
                        pass
        k32.CloseHandle(h)

    def stop(self):
        self._stop.set()


def load_font(size):
    for p in (r'C:\Windows\Fonts\consola.ttf', r'C:\Windows\Fonts\arial.ttf'):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


F_BIG = load_font(44)
F_MED = load_font(20)
F_SML = load_font(14)


class Daemon:
    def __init__(self):
        self.mode = 'ui'
        self.page = 0
        self.events = []
        self.sse = []
        self.lock = threading.RLock()
        self.fip = None
        self.reader = None
        self.video_stop = threading.Event()
        self.status = 'demarrage'

    def start(self):
        self.reader = ButtonReader(self.button_event)
        if os.environ.get('FIPD_SKIP_INIT') == '1':
            ok = True
            self.status = 'init saute (FIPD_SKIP_INIT)'
            self.reader.start()      # lecteur HID immediat
        else:
            self.status = 'activation firmware via SPAD...'
            ok = ensure_firmware_active(reader=self.reader)
        self.status = 'firmware ' + ('active' if ok else 'NON active')
        self.reader = ButtonReader(self.button_event)
        if ok and not self.reader.is_alive():
            pass  # deja demarre par ensure_firmware_active
        try:
            import notrefirmware as nfw
            self.fip = nfw.NotreFirmware()
            self.fip.session()
            self.status += ' — affichage OK (backend natif pur)'
        except Exception as e:
            self.status += ' — affichage ERREUR ' + str(e)
        threading.Thread(target=self._ui_loop, daemon=True).start()

    def button_event(self, state, changed, names):
        ev = {'t': time.time(), 'state': state, 'changed': changed, 'names': names}
        self.events.append(ev)
        self.events = self.events[-200:]
        for q in list(self.sse):
            try:
                q.append(ev)
            except Exception:
                self.sse.remove(q)
        if self.mode == 'ui':
            if changed & 0x08:      # left_ccw : page suivante
                self.page = (self.page + 1) % 3
            elif changed & 0x10:    # left_cw : page precedente
                self.page = (self.page - 1) % 3
        elif self.mode == 'video':
            if changed & 0x20:      # bouton 1 : stop video
                self.video_stop.set()

    def _send(self, img):
        img = img.convert('RGB').resize((320, 240)).transpose(Image.FLIP_TOP_BOTTOM)
        raw = bytearray(img.tobytes())
        raw[0::3], raw[2::3] = raw[2::3], raw[0::3]
        with self.lock:
            return self.fip.display(bytes(raw)) if self.fip else -1

    def show_image(self, img, mode='image'):
        self.mode = mode
        return self._send(img)

    def _ui_loop(self):
        while True:
            if self.mode == 'ui' and self.fip:
                try:
                    self._send(self.render_page())
                except Exception:
                    pass
                time.sleep(1.0)
            else:
                time.sleep(0.4)

    def render_page(self):
        img = Image.new('RGB', (320, 240), (18, 22, 38))
        d = ImageDraw.Draw(img)
        if self.page == 0:
            d.text((160, 90), time.strftime('%H:%M:%S'), font=F_BIG,
                   fill=(255, 255, 255), anchor='mm')
            d.text((160, 140), time.strftime('%A %d %B %Y'), font=F_MED,
                   fill=(180, 220, 255), anchor='mm')
        elif self.page == 1:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            d.text((15, 12), 'SYSTEME', font=F_MED, fill=(255, 210, 80))
            d.text((15, 45), 'CPU  {:5.1f} %'.format(cpu), font=F_MED, fill=(255, 255, 255))
            d.rectangle((15, 75, 305, 87), outline=(90, 90, 90))
            d.rectangle((15, 75, 15 + int(290 * cpu / 100), 87), fill=(80, 200, 120))
            d.text((15, 105), 'RAM  {:5.1f} %'.format(mem), font=F_MED, fill=(255, 255, 255))
            d.rectangle((15, 135, 305, 147), outline=(90, 90, 90))
            d.rectangle((15, 135, 15 + int(290 * mem / 100), 147), fill=(200, 120, 80))
            disk = psutil.disk_usage('C:').percent
            d.text((15, 165), 'Disque C: {:.0f} %'.format(disk), font=F_SML, fill=(200, 200, 200))
        else:
            d.text((15, 12), 'RESEAU', font=F_MED, fill=(255, 210, 80))
            y = 48
            io2 = psutil.net_io_counters()
            d.text((15, y), 'Envoyes {:8.1f} Mo'.format(io2.bytes_sent / 1e6),
                   font=F_SML, fill=(255, 255, 255))
            y += 26
            d.text((15, y), 'Recus   {:8.1f} Mo'.format(io2.bytes_recv / 1e6),
                   font=F_SML, fill=(255, 255, 255))
            y += 34
            for nm, addrs in list(psutil.net_if_addrs().items())[:4]:
                ip = next((a.address for a in addrs if a.family.name == 'AF_INET'), None)
                if ip:
                    d.text((15, y), '{:12s} {}'.format(nm[:12], ip),
                           font=F_SML, fill=(170, 200, 170))
                    y += 22
        d.text((160, 228), 'page {}/3  (knob gauche = page)'.format(self.page + 1),
               font=F_SML, fill=(120, 120, 130), anchor='mm')
        return img

    def play_video(self, path, fps=14):
        import cv2
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise RuntimeError('video illisible')
        self.mode = 'video'
        self.video_stop.clear()
        n = 0
        try:
            while not self.video_stop.is_set():
                ret, frame = cap.read()
                if not ret:
                    break
                img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                self._send(img)
                n += 1
                time.sleep(1.0 / fps)
        finally:
            cap.release()
            self.mode = 'ui'
        return {'frames': n, 'stopped': self.video_stop.is_set()}


D = Daemon()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == '/buttons':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            q = []
            D.sse.append(q)
            try:
                while True:
                    while q:
                        self.wfile.write(('data: ' + json.dumps(q.pop(0)) + '\n\n').encode())
                    self.wfile.flush()
                    time.sleep(0.1)
            except Exception:
                pass
            finally:
                if q in D.sse:
                    D.sse.remove(q)
        elif self.path == '/events':
            self._json(D.events[-50:])
        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            body = ('<html><body style="font-family:sans-serif;background:#111;color:#eee">'
                    '<h2>FIPd v2</h2><p>Status : ' + D.status + '</p><p>Mode : ' + D.mode +
                    '</p><p>POST /screen (image) - POST /video (fichier) - POST /ui - '
                    'GET /buttons (SSE)</p></body></html>').encode()
            self.wfile.write(body)
        else:
            self._json({'error': 'inconnu'}, 404)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        try:
            if self.path.startswith('/screen'):
                if 'url=' in self.path:
                    import urllib.request
                    body = urllib.request.urlopen(self.path.split('url=', 1)[1], timeout=10).read()
                if len(body) == 320 * 240 * 3:
                    D.show_image(Image.frombytes('RGB', (320, 240), body))
                else:
                    D.show_image(Image.open(io.BytesIO(body)))
                return self._json({'ok': True, 'mode': D.mode})
            if self.path == '/video':
                import tempfile
                suffix = os.path.splitext(self.headers.get('X-Filename', ''))[1] or '.mp4'
                tmp = os.path.join(tempfile.gettempdir(), 'fipd_vid' + suffix)
                with open(tmp, 'wb') as f:
                    f.write(body)
                r = D.play_video(tmp)
                return self._json({'ok': True, 'frames': r['frames'], 'stopped': r['stopped']})
            if self.path == '/ui':
                D.mode = 'ui'
                return self._json({'ok': True, 'mode': D.mode})
            if self.path == '/clear':
                with D.lock:
                    hr = 0 if D.fip else -1
                return self._json({'ok': hr == 0})
            if self.path == '/led':
                dd = json.loads(body or b'{}')
                if D.fip:
                    if 'index' in dd:
                        hr = 0
                    else:
                        for i in range(1, 7):
                            hr = 0
                    return self._json({'ok': hr == 0})
                return self._json({'ok': False}, 500)
            return self._json({'error': 'inconnu'}, 404)
        except Exception as e:
            return self._json({'error': str(e)}, 500)


if __name__ == '__main__':
    print('fipd v2 — initialisation (SPAD ~25 s)...', flush=True)
    D.start()
    print('Pret : http://127.0.0.1:{} — {}'.format(PORT, D.status), flush=True)
    print('Interface integree active. Knob GAUCHE = changer de page.', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
