"""
cameras.py — mur de cameras : chaque bouton ouvre un flux video en direct
sur l'ecran du FIP (MJPEG HTTP, RTSP, fichier... tout ce qu'OpenCV lit).

Choix du flux :
  1) sur l'appareil : liste alignee sur S1-S6 (molette droite = defilement),
     bouton = plein ecran ; en direct, molette droite = camera suivante,
     re-appuyer sur le bouton de la camera courante = retour a la liste.
  2) a distance via l'API du daemon :
       GET  /app/cameras                    -> liste
       POST /app/cameras {"action":"add","name":"Salon","url":"http://..."}
       POST /app/cameras {"action":"remove","name":"Salon"}
     La liste est persistee dans apps/cameras.json (cree vide au 1er lancement).
NB : pour du RTSP derriere un NAT qui Decoupe, OpenCV est configure en TCP.
"""
import json
import os
import threading
import time

os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS', 'rtsp_transport;tcp')

import fipui
from fipui import App

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cameras.json')
FPS = 4.5                        # affichage en direct (~4-5 im/s sur le FIP)


class CamerasApp(App):
    name = 'cameras'
    title = 'CAMERAS'
    show_footer = False

    def __init__(self, daemon):
        super().__init__(daemon)
        self.cams = []               # [{'name', 'url'}]
        self._load()
        if not os.path.exists(CFG):
            self._save()
        self.mode = 'grid'           # grid | live
        self.live = None             # index de la camera en direct
        self.view = 0                # 1re ligne visible (grid)
        self.frame = None            # derniere image PIL RGB
        self.err = None
        self._frame_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.on = False              # la page est-elle affichee ?

    # ---------- config ----------

    def _load(self):
        try:
            with open(CFG, encoding='utf-8') as f:
                self.cams = json.load(f)
            if not isinstance(self.cams, list):
                self.cams = []
        except OSError:
            self.cams = []
        except Exception as e:
            print('[cameras] config illisible:', e, flush=True)
            self.cams = []

    def _save(self):
        try:
            with open(CFG, 'w', encoding='utf-8') as f:
                json.dump(self.cams, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print('[cameras] ecriture config:', e, flush=True)

    def http_get(self):
        with self._frame_lock:
            live_ok = self.frame is not None
        return {'cameras': self.cams, 'mode': self.mode, 'live': self.live,
                'frame_ok': live_ok}

    def http_post(self, p):
        act = p.get('action')
        if act == 'add':
            name, url = str(p.get('name', '')).strip(), str(p.get('url', '')).strip()
            if not name or not url:
                return {'error': 'name et url requis'}
            self.cams = [c for c in self.cams if c['name'] != name]
            self.cams.append({'name': name, 'url': url})
            self._save()
            return {'ok': True, 'cameras': self.cams}
        if act == 'remove':
            self.cams = [c for c in self.cams if c['name'] != p.get('name')]
            self._save()
            if self.mode == 'live' and self.live is not None \
                    and self.live >= len(self.cams):
                self._stop_live()
            return {'ok': True, 'cameras': self.cams}
        return {'error': 'action add|remove'}

    # ---------- thread de capture ----------

    def _start_live(self, idx):
        if not self.cams:
            self.alert('aucune camera')
            return
        self._stop_live()
        self.live = max(0, min(idx, len(self.cams) - 1))
        self.mode = 'live'
        self._stop = threading.Event()
        url = self.cams[self.live]['url']       # fige avant le thread (race)
        self._thread = threading.Thread(target=self._grab,
                                        args=(self._stop, url), daemon=True)
        self._thread.start()

    def _stop_live(self):
        self._stop.set()
        self._thread = None
        self.mode = 'grid'
        self.live = None
        with self._frame_lock:
            self.frame = None
            self.err = None

    def _grab(self, stop, url):
        """Lit le flux en boucle ; un changement de camera = nouveau thread
        (_start_live arrete celui-ci), donc url est un parametre fige."""
        import cv2
        from PIL import Image
        cap = None
        last_reopen = 0.0
        while not stop.is_set():
            if cap is None:
                if time.time() - last_reopen < 2.0:
                    stop.wait(0.5)
                    continue
                last_reopen = time.time()
                try:
                    cap = cv2.VideoCapture(url)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        with self._frame_lock:
                            self.err = 'flux illisible'
                except Exception as e:
                    cap = None
                    with self._frame_lock:
                        self.err = str(e)[:40]
            if cap is not None:
                ret, fr = cap.read()
                if ret:
                    img = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
                    with self._frame_lock:
                        self.frame = img
                        self.err = None
                    stop.wait(1.0 / FPS)
                    self.d._redraw_now()
                    continue
                cap.release()                      # flux coupe : reconnexion
                cap = None
                with self._frame_lock:
                    self.err = 'flux coupe'
            stop.wait(1.0)

    # ---------- cycle de vie ----------

    def on_enter(self):
        self.on = True
        if self.mode == 'live' and self.live is not None:
            self._start_live(self.live)

    def on_leave(self):
        self.on = False
        self._stop_live()

    def on_event(self, name, pressed):
        if not pressed:
            return
        if name == 'right_cw':
            self._knob(+1)
            return
        if name == 'right_ccw':
            self._knob(-1)
            return
        if not (name.startswith('S') and name[1:].isdigit()):
            return
        n = int(name[1]) - 1
        if self.mode == 'grid':
            idx = self.view + n
            if idx < len(self.cams):
                self._start_live(idx)
        else:
            if n == self.live:                     # meme camera = retour grille
                self._stop_live()
                self.alert('liste')
            elif n < len(self.cams):
                self._start_live(n)

    def _knob(self, d):
        n = len(self.cams)
        if self.mode == 'grid':
            if n > 6:
                self.view = max(0, min(self.view + d, n - 6))
        elif n:
            self._start_live((self.live + d) % n)

    # ---------- rendu ----------

    def render_full(self, img):
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        F_S = fipui.font(20)
        F_XS = fipui.font(14)
        if self.mode == 'live':
            cam = self.cams[self.live] if self.live < len(self.cams) \
                else {'name': '?'}
            with self._frame_lock:
                frame, err = self.frame, self.err
            if frame is not None:
                fr = frame.copy()
                r = max(320 / fr.width, 240 / fr.height)
                fr = fr.resize((int(fr.width * r) + 1, int(fr.height * r) + 1))
                x, y = (fr.width - 320) // 2, (fr.height - 240) // 2
                img.paste(fr.crop((x, y, x + 320, y + 240)), (0, 0))
                d = ImageDraw.Draw(img, 'RGBA')
                d.rectangle((0, 0, 319, 21), fill=(0, 0, 0, 150))
            else:
                d.rectangle((0, 90, 319, 150), fill=(40, 16, 16))
                d.text((160, 106), err or 'connexion...', font=F_S,
                       fill=fipui.RED, anchor='mm')
                d.text((160, 132), cam['name'][:24], font=F_XS, fill=fipui.DIM,
                       anchor='mm')
            d.ellipse((8, 7, 16, 15), fill=fipui.RED)          # point "REC"
            d.text((24, 11), 'DIRECT ' + cam['name'][:20], font=F_XS,
                   fill=(255, 255, 255), anchor='lm')
            d.text((312, 11), '{}/{}'.format(self.live + 1, len(self.cams)),
                   font=F_XS, fill=(255, 255, 255), anchor='rm')
            d.text((160, 232), 'molette = camera - S = changer - S courant = liste',
                   font=F_XS, fill=(255, 255, 255), anchor='mm')
            return
        # grille
        d.rectangle((0, 0, 319, 23), fill=(28, 33, 50))
        d.text((8, 12), 'CAMERAS', font=F_S, fill=fipui.ACCENT, anchor='lm')
        d.text((312, 12), '{} flux'.format(len(self.cams)), font=F_XS,
               fill=fipui.DIM, anchor='rm')
        if not self.cams:
            d.text((160, 110), 'Aucun flux configure', font=F_S, fill=fipui.DIM,
                   anchor='mm')
            d.text((160, 140), 'POST /app/cameras', font=F_XS, fill=fipui.DIM,
                   anchor='mm')
            d.text((160, 158), '{"action":"add","name":"Salon",', font=F_XS,
                   fill=(90, 95, 110), anchor='mm')
            d.text((160, 174), ' "url":"http://ip/video"}', font=F_XS,
                   fill=(90, 95, 110), anchor='mm')
            return
        for r in range(6):
            i = self.view + r
            if i >= len(self.cams):
                break
            y = 26 + r * 35
            yc = y + 17
            if r:
                d.line((0, y - 9, 319, y - 9), fill=(32, 38, 56))
            d.text((16, yc - 8), self.cams[i]['name'][:26], font=F_S,
                   fill=fipui.FG, anchor='lm')
            url = self.cams[i]['url']
            if d.textlength(url, font=F_XS) > 250:
                while url and d.textlength(url + '...', font=F_XS) > 250:
                    url = url[:-1]
                url += '...'
            d.text((16, yc + 9), url, font=F_XS, fill=(90, 95, 110),
                   anchor='lm')
            d.text((316, yc), 'S{}'.format(r + 1), font=F_XS,
                   fill=(90, 95, 110), anchor='rm')
        if len(self.cams) > 6:
            d.rectangle((317, 26, 319, 238), fill=(30, 35, 50))
            th = 212 * 6 // len(self.cams)
            ty = 26 + (212 - th) * self.view // max(1, len(self.cams) - 6)
            d.rectangle((317, ty, 319, ty + th), fill=fipui.BLUE)
