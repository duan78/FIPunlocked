"""
webradio.py — le FIP devient une radio de bureau : le son sort par les
haut-parleurs du PC, l'ecran affiche la station et le morceau en cours,
la molette sert de bouton d'accord.

  molette droite = accorder la station suivante / precedente (et jouer)
  S1..S6        = presets (6 premieres stations ; re-appuyer = couper le son)
  LED 7         = allumee quand ca joue

Catalogue : apps/radio.json, cree au 1er lancement avec les stations
francaises les plus ecoutees de radio-browser.info (base communautaire
libre, ~50 000 stations). Hors ligne : liste de secours integree.
API du daemon :
  GET  /app/webradio                       -> etat (station, titre, en jeu)
  POST /app/webradio {"action":"add","name":"FIP Rock","url":"https://..."}
  POST /app/webradio {"action":"play","index":N}   / {"action":"stop"}
Lecture : ffplay (ffmpeg) en arriere-plan ; le titre du morceau est lu dans
les metadonnees ICY du flux ("Stream title: ...").
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

import fipui
from fipui import App

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'radio.json')
CREATE_NO_WINDOW = 0x08000000
UA = {'User-Agent': 'fipd-webradio/1.0'}
RB_MIRRORS = ['https://de1.api.radio-browser.info',
              'https://fi1.api.radio-browser.info',
              'http://all.api.radio-browser.info']

# liste de secours si radio-browser est injoignable (flux officiels stables)
FALLBACK = [
    {'name': 'FIP', 'url': 'https://icecast.radiofrance.fr/fip-midfi.mp3',
     'tags': 'eclectic', 'codec': 'MP3', 'bitrate': 128},
    {'name': 'France Inter', 'url': 'https://icecast.radiofrance.fr/franceinter-midfi.mp3',
     'tags': 'general', 'codec': 'MP3', 'bitrate': 128},
    {'name': 'franceinfo', 'url': 'https://icecast.radiofrance.fr/franceinfo-midfi.mp3',
     'tags': 'news', 'codec': 'MP3', 'bitrate': 128},
    {'name': 'France Musique', 'url': 'https://icecast.radiofrance.fr/francemusique-midfi.mp3',
     'tags': 'classical', 'codec': 'MP3', 'bitrate': 128},
    {'name': 'France Culture', 'url': 'https://icecast.radiofrance.fr/franceculture-midfi.mp3',
     'tags': 'talk', 'codec': 'MP3', 'bitrate': 128},
    {'name': 'Mouv', 'url': 'https://icecast.radiofrance.fr/mouv-midfi.mp3',
     'tags': 'urban', 'codec': 'MP3', 'bitrate': 128},
    {'name': 'TSF Jazz', 'url': 'https://tsfjazz.ice.infomaniak.ch/tsfjazz-high.mp3',
     'tags': 'jazz', 'codec': 'MP3', 'bitrate': 192},
    {'name': 'SomaFM Groove Salad', 'url': 'https://ice1.somafm.com/groovesalad-128-mp3',
     'tags': 'downtempo', 'codec': 'MP3', 'bitrate': 128},
]

ICY_RE = re.compile(rb"Stream title: ?'?(.*?)'?\s*$")


class WebRadioApp(App):
    name = 'webradio'
    title = 'RADIO'
    show_footer = False

    def __init__(self, daemon):
        super().__init__(daemon)
        self.stations = []
        self.idx = 0                  # station accordee
        self.playing = False
        self.icy = None               # titre du morceau en cours
        self.status = 'chargement...'
        self._proc = None
        self._reader = None
        self._stop = threading.Event()
        threading.Thread(target=self._init_catalog, daemon=True).start()

    # ---------- catalogue ----------

    def _init_catalog(self):
        self._load()
        if self.stations:
            self.status = '{} stations'.format(len(self.stations))
            return
        st = self._fetch_stations()
        self.stations = st or FALLBACK
        self._save()
        self.status = '{} stations{}'.format(
            len(self.stations), '' if st else ' (hors ligne)')

    def _load(self):
        try:
            with open(CFG, encoding='utf-8') as f:
                cfg = json.load(f)
            self.stations = cfg.get('stations') or []
        except OSError:
            pass
        except Exception as e:
            print('[webradio] config illisible:', e, flush=True)

    def _save(self):
        try:
            with open(CFG, 'w', encoding='utf-8') as f:
                json.dump({'stations': self.stations}, f, indent=2,
                          ensure_ascii=False)
        except OSError as e:
            print('[webradio] ecriture config:', e, flush=True)

    @staticmethod
    def _fetch_stations():
        import requests
        for base in RB_MIRRORS:
            try:
                r = requests.get(
                    base + '/json/stations/search',
                    params={'countrycode': 'FR', 'order': 'votes',
                            'reverse': 'true', 'limit': 12, 'hidebroken': 'true'},
                    headers=UA, timeout=8)
                r.raise_for_status()
                out = []
                for s in r.json():
                    out.append({'name': s.get('name', '?')[:28],
                                'url': s.get('url_resolved') or s.get('url'),
                                'tags': (s.get('tags') or '')[:40],
                                'codec': s.get('codec'), 'bitrate': s.get('bitrate')})
                out = [s for s in out if s['url']]
                if out:
                    return out
            except Exception:
                continue
        return None

    # ---------- lecture ----------

    def _play(self, idx):
        self._kill()
        if not self.stations:
            return
        self.idx = max(0, min(idx, len(self.stations) - 1))
        st = self.stations[self.idx]
        ffplay = shutil.which('ffplay')
        if not ffplay:
            self.status = 'ffplay introuvable (ffmpeg)'
            self.alert('installez ffmpeg')
            return
        try:
            self._proc = subprocess.Popen(
                [ffplay, '-hide_banner', '-nostats', '-nodisp',
                 '-loglevel', 'info', '-i', st['url']],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.status = str(e)[:40]
            return
        self.playing = True
        self.icy = None
        self.set_led(7, True)
        self.status = 'en jeu'
        self._reader = threading.Thread(target=self._read_icy,
                                        args=(self._proc,), daemon=True)
        self._reader.start()
        self.d._redraw_now()

    def _kill(self):
        self.playing = False
        self.icy = None
        self.set_led(7, False)
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _read_icy(self, proc):
        """Lit stderr de ffplay : lignes "Stream title: 'Artiste - Titre'"."""
        try:
            for raw in iter(proc.stderr.readline, b''):
                if self._stop.is_set() or proc is not self._proc:
                    break
                m = ICY_RE.search(raw.strip())
                if m:
                    self.icy = m.group(1).decode('utf-8', 'replace')[:80]
                    self.d._redraw_now()
        except Exception:
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def on_leave(self):
        self._kill()                    # pas de radio fantome sur une autre page

    # ---------- interactions ----------

    def on_event(self, name, pressed):
        if not pressed or not self.stations:
            return
        if name == 'right_cw':
            self._play(self.idx + 1 if self.idx < len(self.stations) - 1 else 0)
        elif name == 'right_ccw':
            self._play(self.idx - 1 if self.idx > 0 else len(self.stations) - 1)
        elif name in ('S1', 'S2', 'S3', 'S4', 'S5', 'S6'):
            idx = int(name[1]) - 1
            if idx >= len(self.stations):
                return
            if self.playing and idx == self.idx:
                self._kill()
                self.status = 'arrete'
            else:
                self._play(idx)

    def http_get(self):
        return {'stations': self.stations, 'index': self.idx,
                'playing': self.playing, 'icy_title': self.icy,
                'status': self.status}

    def http_post(self, p):
        act = p.get('action')
        if act == 'add':
            name, url = str(p.get('name', '')).strip(), str(p.get('url', '')).strip()
            if not name or not url:
                return {'error': 'name et url requis'}
            self.stations.append({'name': name[:28], 'url': url,
                                  'tags': p.get('tags', ''), 'codec': '', 'bitrate': 0})
            self._save()
            return {'ok': True, 'stations': len(self.stations)}
        if act == 'play':
            self._play(int(p.get('index', self.idx)))
            return {'ok': True, 'playing': self.playing}
        if act == 'stop':
            self._kill()
            return {'ok': True}
        return {'error': 'action add|play|stop'}

    # ---------- rendu ----------

    def render(self, d):
        F_L = fipui.font(30)
        F_S = fipui.font(20)
        F_XS = fipui.font(14)
        d.rectangle((0, 0, 319, 23), fill=(28, 33, 50))
        d.text((8, 12), 'RADIO', font=F_S, fill=fipui.ACCENT, anchor='lm')
        d.text((312, 12), self.status[:20], font=F_XS, fill=fipui.DIM,
               anchor='rm')
        if not self.stations:
            d.text((160, 110), 'Catalogue en chargement...', font=F_S,
                   fill=fipui.DIM, anchor='mm')
            d.text((160, 140), '(radio-browser.info)', font=F_XS,
                   fill=fipui.DIM, anchor='mm')
            return
        st = self.stations[self.idx]
        # nom de la station (2 lignes max)
        name = st['name']
        if d.textlength(name, font=F_L) > 300:
            d.text((160, 55), name[:20], font=F_L, fill=fipui.FG, anchor='mm')
            d.text((160, 90), name[20:40], font=F_L, fill=fipui.FG, anchor='mm')
        else:
            d.text((160, 70), name, font=F_L, fill=fipui.FG, anchor='mm')
        meta = ' '.join(x for x in
                        ([str(st.get('bitrate') or '') + 'k'] if st.get('bitrate') else [])
                        + [str(st.get('codec') or '')]) or ' '
        tags = (st.get('tags') or '').split(',')[0]
        d.text((160, 105), (meta + '  ' + tags).strip()[:34], font=F_XS,
               fill=(100, 140, 200), anchor='mm')
        # morceau en cours (ICY) ou etat
        d.rectangle((8, 122, 311, 178), fill=(20, 24, 36), outline=(40, 46, 64))
        if self.playing:
            d.ellipse((16, 140, 26, 150), fill=fipui.GREEN)
            d.text((34, 145), 'EN DIRECT', font=F_XS, fill=fipui.GREEN,
                   anchor='lm')
            title = self.icy or 'metadonnees du flux...'
            for i, line in enumerate([title[j:j + 38] for j in range(0, len(title), 38)][:2]):
                d.text((16, 158 + i * 15), line, font=F_XS,
                       fill=fipui.FG if self.icy else fipui.DIM, anchor='lm')
        else:
            d.text((160, 150), 'molette droite = accorder', font=F_XS,
                   fill=fipui.DIM, anchor='mm')
        # presets visibles alignes sur S1-S6 (S<n> = stations[n-1])
        for r in range(6):
            x = 12 + r * 51
            on = self.playing and self.idx == r
            d.rectangle((x, 192, x + 47, 222),
                        fill=(26, 46, 40) if on else (22, 26, 40),
                        outline=fipui.GREEN if on else (50, 56, 76))
            d.text((x + 23, 207), 'S{}'.format(r + 1), font=F_S,
                   fill=fipui.GREEN if on else fipui.DIM, anchor='mm')
        d.text((160, 230), 'molette = tuner - re-appuyer S = couper',
               font=F_XS, fill=(120, 120, 130), anchor='mm')
