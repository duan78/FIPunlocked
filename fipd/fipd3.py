"""
fipd v3 — Flight Instrument Panel : daemon generique via la pile officielle.

Backend : DirectOutput.dll (client pipes) -> DirectOutputService (service Logitech,
installe une fois pour toutes). Ecran + 6 boutons + 2 molettes, sans SPAD ni licence.

Prerequis : service DirectOutputService demarre (voir README).
  (demarrage : sc start DirectOutputService  — droits admin)

API http://127.0.0.1:8199 (identique v2) :
  POST /screen (PNG/JPG/BMP/WEBP ou ?url=)   affiche une image
  POST /video (fichier)                      joue une video (bouton S1 = stop)
  POST /ui                                   interface integree (molette gauche = page)
  POST /clear   GET /buttons (SSE) /events /
"""
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ctypes
import ctypes.wintypes as wt

PORT = 8199
DO_DIR = r'C:\Program Files\Logitech\DirectOutput'

os.chdir(DO_DIR)
do = ctypes.WinDLL(os.path.join(DO_DIR, 'DirectOutput.dll'))

_hr = ['DirectOutput_Initialize', 'DirectOutput_Deinitialize',
       'DirectOutput_RegisterDeviceCallback', 'DirectOutput_Enumerate',
       'DirectOutput_RegisterPageCallback', 'DirectOutput_RegisterSoftButtonCallback',
       'DirectOutput_AddPage', 'DirectOutput_RemovePage', 'DirectOutput_SetLed',
       'DirectOutput_SetImage', 'DirectOutput_SetImageFromFile', 'DirectOutput_SetProfile']
for n in _hr:
    getattr(do, n).restype = ctypes.HRESULT

ENUM_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
BTN_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p)

VOID_P = ctypes.c_void_p
do.DirectOutput_Enumerate.argtypes = [ENUM_CB, VOID_P]
do.DirectOutput_RegisterSoftButtonCallback.argtypes = [VOID_P, BTN_CB, VOID_P]
do.DirectOutput_AddPage.argtypes = [VOID_P, wt.DWORD, wt.LPCWSTR, wt.DWORD]
do.DirectOutput_SetImage.argtypes = [VOID_P, wt.DWORD, wt.DWORD, wt.DWORD, VOID_P]

SOFTBUTTONS = {0x01: 'select', 0x02: 'right_cw', 0x04: 'right_ccw', 0x08: 'left_ccw',
               0x10: 'left_cw', 0x20: 'S1', 0x40: 'S2', 0x80: 'S3',
               0x100: 'S4', 0x200: 'S5', 0x400: 'S6'}


def load_font(size):
    from PIL import ImageFont
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
        self.hdev = None
        self.status = 'demarrage'
        self.video_stop = threading.Event()

    # ---------- demarrage pile officielle ----------

    def start(self, timeout=20):
        hr = do.DirectOutput_Initialize('fipd')
        if hr != 0:
            self.status = f'Initialize hr=0x{hr & 0xFFFFFFFF:08X}'
            return False

        found = []

        @ENUM_CB
        def on_enum(h, ctx):
            found.append(h)

        do.DirectOutput_Enumerate(on_enum, None)
        if not found:
            self.status = 'aucun peripherique DirectOutput'
            return False
        self.hdev = found[0]

        self._btn_cb = BTN_CB(self._on_btn)   # reference gardee : le service l'appellera
        do.DirectOutput_RegisterSoftButtonCallback(self.hdev, self._btn_cb, None)
        hr = do.DirectOutput_AddPage(self.hdev, 0, 'fipd', 1)  # FLAG_SET_AS_ACTIVE
        self.status = f'OK (device 0x{self.hdev:x}, AddPage hr=0x{hr & 0xFFFFFFFF:08X})'
        threading.Thread(target=self._keepalive, daemon=True).start()
        return True

    def _on_btn(self, h, buttons, ctx):
        names = [n for b, n in SOFTBUTTONS.items() if buttons & b]
        ev = {'t': time.time(), 'state': buttons, 'names': names}
        self.events.append(ev)
        self.events = self.events[-200:]
        for q in list(self.sse):
            try:
                q.append(ev)
            except Exception:
                self.sse.remove(q)
        if self.mode == 'ui':
            if buttons & 0x08:            # left_ccw
                self.page = (self.page + 1) % 3
            elif buttons & 0x10:          # left_cw
                self.page = (self.page - 1) % 3
        elif self.mode == 'video':
            if buttons & 0x20:            # S1
                self.video_stop.set()

    # ---------- affichage ----------

    def _send(self, img):
        from PIL import Image
        img = img.convert('RGB').resize((320, 240)).transpose(Image.FLIP_TOP_BOTTOM)
        buf = ctypes.create_string_buffer(img.tobytes(), 320 * 240 * 3)
        self._last_raw = buf
        do.DirectOutput_SetImage(self.hdev, 0, 0, 320 * 240 * 3, buf)

    def show_image(self, img, mode='image'):
        self.mode = mode
        self._send(img)

    def _keepalive(self):
        """Reaffiche la derniere image /2 s : garde la session firmware active."""
        while True:
            if self.hdev:
                try:
                    if self.mode == 'ui':
                        self._send(self.render_page())
                    elif getattr(self, '_last_raw', None):
                        do.DirectOutput_SetImage(self.hdev, 0, 0, 320 * 240 * 3,
                                                 self._last_raw)
                except Exception:
                    pass
            time.sleep(2.0)

    def render_page(self):
        import psutil
        from PIL import Image, ImageDraw
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
        d.text((160, 228), 'page {}/3  (molette gauche)'.format(self.page + 1),
               font=F_SML, fill=(120, 120, 130), anchor='mm')
        return img

    def play_video(self, path, fps=14):
        import cv2
        from PIL import Image
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
                self._send(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
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
                    '<h2>FIPd v3</h2><p>Status : ' + D.status + '</p><p>Mode : ' + D.mode +
                    '</p><p>POST /screen (image) - POST /video - POST /ui - '
                    'GET /buttons (SSE)</p></body></html>').encode()
            self.wfile.write(body)
        else:
            self._json({'error': 'inconnu'}, 404)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        try:
            from PIL import Image
            if self.path.startswith('/screen'):
                if 'url=' in self.path:
                    import urllib.request
                    body = urllib.request.urlopen(self.path.split('url=', 1)[1], timeout=10).read()
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
            return self._json({'error': 'inconnu'}, 404)
        except Exception as e:
            return self._json({'error': str(e)}, 500)


def main():
    import sys
    print('fipd v3 — connexion a la pile officielle (service DirectOutputService)...', flush=True)
    if not D.start():
        print('ECHEC :', D.status, flush=True)
        print("Demarrez le service : sc start DirectOutputService  (admin)", flush=True)
        sys.exit(1)
    print('Pret : http://127.0.0.1:8199 —', D.status, flush=True)
    print('Interface integree : molette GAUCHE = page. S1 = stop video.', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()


if __name__ == '__main__':
    main()
