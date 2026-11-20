"""
fipd.py — Daemon HTTP : votre Logitech Flight Instrument Panel en périphérique générique.

Backend : DirectOutputSaiFip.dll (officielle, appelée par ctypes) — gère l'ouverture,
l'initialisation du firmware et l'affichage. Aucun service Logitech requis.

API locale (127.0.0.1:8199) :
  GET  /                     page d'etat
  POST /screen               corps = PNG/JPG/BMP/WEBP (ou raw 230400 o BGR) -> affiche
  POST /screen?url=...       affiche une image telechargee
  POST /clear                efface l'ecran
  POST /led                  {"index":1-6,"on":true} (ACK, selon materiel)
  POST /brightness-raw       {"value":0-255} (commande brute SET_LCD)
  GET  /buttons              SSE : evenements boutons (si le firmware les emet)
  GET  /events               JSON des derniers evenements

Usage :
  SPAD.neXt ferme, puis :  python fipd.py
Test :
  curl -X POST --data-binary @photo.png http://127.0.0.1:8199/screen
"""
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

import fip_native
from fip_native import FipNative, SOFTBUTTONS

PORT = 8199


class State:
    def __init__(self):
        self.fip = None
        self.events = []
        self.sse = []
        self.status = 'non connecte'
        self.lock = threading.Lock()
        self.connect()

    def connect(self):
        try:
            self.fip = FipNative()
            self.fip.on_buttons = self.button
            self.fip.dll.DirectOutputDriver_RegisterSoftButtonCallback(
                self.fip.handle, self.fip._cb, None)
            self.fip.set_number_of_pages(1)
            self.fip.activate_default_page()
            self.status = 'connecte (backend natif DLL)'
            return True
        except Exception as e:
            self.status = f'erreur: {e}'
            return False

    def button(self, state, changed, names):
        ev = {'t': time.time(), 'state': state, 'changed': changed, 'names': names}
        self.events.append(ev)
        self.events = self.events[-200:]
        for q in list(self.sse):
            try:
                q.append(ev)
            except Exception:
                self.sse.remove(q)

    def show(self, img: Image.Image):
        img = img.convert('RGB').resize((320, 240)).transpose(Image.FLIP_TOP_BOTTOM)
        raw = bytearray(img.tobytes())
        raw[0::3], raw[2::3] = raw[2::3], raw[0::3]
        self._last_raw = bytes(raw)      # pour le keepalive
        with self.lock:
            hr = self.fip.set_image(self._last_raw)
        if hr != 0:
            raise RuntimeError(f'SetImage hr=0x{hr & 0xFFFFFFFF:08X}')

    _last_raw = None

    def start_keepalive(self, period=4.0):
        """Le firmware retombe sur son ecran d'accueil apres inactivite :
        on re-affiche la derniere image periodiquement."""
        def loop():
            while True:
                time.sleep(period)
                raw = self._last_raw
                if raw and self.fip:
                    try:
                        with self.lock:
                            self.fip.set_image(raw)
                    except Exception:
                        pass
        threading.Thread(target=loop, daemon=True).start()


S = State()


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
            S.sse.append(q)
            try:
                while True:
                    while q:
                        self.wfile.write(f'data: {json.dumps(q.pop(0))}\n\n'.encode())
                    self.wfile.flush()
                    time.sleep(0.1)
            except Exception:
                pass
            finally:
                if q in S.sse:
                    S.sse.remove(q)
        elif self.path == '/events':
            self._json(S.events[-50:])
        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'''<html><body style="font-family:sans-serif;background:#111;color:#eee">
<h2>FIPd — Flight Instrument Panel</h2>
<p>Status : {S.status}</p>
<p>POST /screen (image) · POST /clear · POST /led · GET /buttons (SSE) · GET /events</p>
</body></html>'''.encode())
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
                    with S.lock:
                        S.fip.set_image(body)
                else:
                    S.show(Image.open(io.BytesIO(body)))
                return self._json({'ok': True})
            if self.path == '/clear':
                with S.lock:
                    hr = S.fip.clear_display()
                return self._json({'ok': hr == 0})
            if self.path == '/led':
                d = json.loads(body or b'{}')
                with S.lock:
                    if 'index' in d:
                        hr = S.fip.set_led(0, int(d['index']), 1 if d.get('on') else 0)
                    else:
                        for i in range(1, 7):
                            hr = S.fip.set_led(0, i, 1 if d.get('on') else 0)
                return self._json({'ok': hr == 0, 'note': 'LED selon materiel'})
            if self.path == '/brightness-raw':
                v = int(json.loads(body or b'{}').get('value', 128))
                with S.lock:
                    hr = S.fip.dll.DirectOutputDriver_SetLed(S.fip.handle, 0, 25, v)
                return self._json({'ok': hr == 0})
            return self._json({'error': 'inconnu'}, 404)
        except Exception as e:
            return self._json({'error': str(e)}, 500)


if __name__ == '__main__':
    print(f'fipd — http://127.0.0.1:{PORT}  ({S.status})')
    if S.fip:
        demo = Image.new('RGB', (320, 240), (12, 12, 35))
        d = ImageDraw.Draw(demo)
        d.rectangle((8, 8, 312, 40), fill=(0, 130, 210))
        d.text((16, 18), 'FIPd operationnel !', fill=(255, 255, 255))
        d.text((16, 60), 'POST /screen -> afficher une image', fill=(150, 220, 255))
        d.text((16, 85), 'curl -X POST --data-binary @img.png', fill=(150, 220, 255))
        d.text((16, 110), 'http://127.0.0.1:8199/screen', fill=(150, 220, 255))
        try:
            S.show(demo)
            S.start_keepalive()
            print('Ecran de demarrage affiche (keepalive actif).')
        except Exception as e:
            print('affichage demo:', e)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
