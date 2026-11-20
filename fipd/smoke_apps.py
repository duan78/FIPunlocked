"""
smoke_apps.py — rend les pages des apps tierces en PNG sans FIP (daemon factice).
Touches neutralisees (aucun keysend), aucun ffplay/camera lance.
Sorties dans _smoke_out/*.png
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import keysend
keysend.tap = lambda *a, **k: None           # aucune touche reelle envoyee
import webbrowser
webbrowser.open = lambda url: print('[smoke] webbrowser:', url)

import fipui
from PIL import Image, ImageDraw


class DummyDaemon:
    def __init__(self):
        self.leds = {}
    def set_led(self, index, on):
        self.leds[index] = bool(on)
    def _redraw_now(self):
        pass


def render(app, name, alert=None):
    img = Image.new('RGB', (320, 240), fipui.BG)
    d = ImageDraw.Draw(img)
    app.on_enter()
    if alert:
        app.alert(alert)
    if hasattr(app, 'render_full'):
        app.render_full(img)
        d = ImageDraw.Draw(img)
        app.draw_alert(d, fipui.font(20))
    else:
        app.render(d)
        app.draw_alert(d, fipui.font(20))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_smoke_out')
    os.makedirs(out, exist_ok=True)
    img.save(os.path.join(out, name + '.png'))
    print('[smoke] OK', name, '- LEDs:', sorted(daemon.leds.items()))


daemon = DummyDaemon()

import importlib.util


def load(modname):
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'apps', modname + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- DECK ---
mod = load('deck')
deck = [a for a in vars(mod).values()
        if isinstance(a, type) and getattr(a, 'name', '') == 'deck'][0](daemon)
render(deck, 'deck')
deck.on_event('right_ccw', True)

# --- NOW PLAYING (SMTC : rien ne joue -> page d'attente) ---
mod = load('nowplaying')
np_ = [a for a in vars(mod).values()
       if isinstance(a, type) and getattr(a, 'name', '') == 'nowplaying'][0](daemon)
time.sleep(3.5)                    # laisse le poll SMTC tourner un coup
render(np_, 'nowplaying')

# --- GITHUB CI ---
mod = load('githubci')
gh = [a for a in vars(mod).values()
      if isinstance(a, type) and getattr(a, 'name', '') == 'githubci'][0](daemon)
time.sleep(12)                     # un cycle de poll (gh run list par repo)
render(gh, 'githubci')
render(gh, 'githubci_alerte', alert='mon-repo: ECHEC CI')

# --- TASK MANAGER ---
mod = load('taskmgr')
tm = [a for a in vars(mod).values()
      if isinstance(a, type) and getattr(a, 'name', '') == 'taskmgr'][0](daemon)
time.sleep(7.5)                    # 1er passage de mesure complet
render(tm, 'taskmgr')
tm.on_event('S1', True)            # arme le kill
render(tm, 'taskmgr_arme')
tm.on_event('right_cw', True)      # change le tri
render(tm, 'taskmgr_tri_disque')

# --- CAMERAS ---
mod = load('cameras')
cam = [a for a in vars(mod).values()
       if isinstance(a, type) and getattr(a, 'name', '') == 'cameras'][0](daemon)
render(cam, 'cameras_vide')
cam.http_post({'action': 'add', 'name': 'Salon',
               'url': 'http://192.168.1.42:8080/video'})
cam.http_post({'action': 'add', 'name': 'Jardin',
               'url': 'rtsp://192.168.1.43:554/stream1'})
cam.http_post({'action': 'add', 'name': 'Portail', 'url': 'http://cam3/mjpeg'})
render(cam, 'cameras_grille')

# --- WEBRADIO (catalogue via reseau ; pas de lecture) ---
mod = load('webradio')
radio = [a for a in vars(mod).values()
         if isinstance(a, type) and getattr(a, 'name', '') == 'webradio'][0](daemon)
time.sleep(10)                     # catalogue radio-browser ou fallback
render(radio, 'webradio')
radio.icy = 'Miles Davis - So What'
radio.playing = True
radio.idx = 0
radio.set_led(7, True)
render(radio, 'webradio_en_jeu')

print('[smoke] terminé — PNG dans _smoke_out/')
