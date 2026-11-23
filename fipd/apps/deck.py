"""
deck.py — "FIP Deck" generique : pages de boutons definies dans apps/deck.json
(recharge a chaud : le fichier est relu a chaque changement de mtime).

Format :
{
  "pages": [
    { "title": "PERSO",
      "buttons": {
        "S1": {"label": "Lecture/Pause", "action": {"type": "key",  "combo": "playpause"}},
        "S2": {"label": "Son",           "action": {"type": "key",  "combo": "volumemute"}},
        "S3": {"label": "GitHub",        "action": {"type": "url",  "url": "https://github.com"}},
        "S4": {"label": "Lumiere",       "action": {"type": "http", "method": "POST",
                                                    "url": "http://...", "json": {"on": true}}},
        "S5": {"label": "Notepad",       "action": {"type": "run",  "cmd": ["notepad"]}},
        "S6": {"label": "Bip",           "action": {"type": "alert", "message": "coucou"}}
      } }
  ]
}

Types d'action : key (combo keysend — GET /keys pour la liste), url (navigateur),
http (method/url/headers/json), run (liste argv), alert (bandeau FIP).
Rendu : 6 rangees de 40 px alignees sur S1-S6 · molette droite = page du deck.
"""
import json
import os
import subprocess
import threading

import fipui
from fipui import App

CREATE_NO_WINDOW = 0x08000000
CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'deck.json')

SAMPLE = {
    "pages": [
        {
            "title": "DECK",
            "buttons": {
                "S1": {"label": "Lecture / Pause", "action": {"type": "key", "combo": "playpause"}},
                "S2": {"label": "Couper le son", "action": {"type": "key", "combo": "volumemute"}},
                "S3": {"label": "Volume +", "action": {"type": "key", "combo": "volumeup"}},
                "S4": {"label": "Volume -", "action": {"type": "key", "combo": "volumedown"}},
                "S5": {"label": "Explorateur", "action": {"type": "key", "combo": "win+e"}},
                "S6": {"label": "Bureau", "action": {"type": "key", "combo": "win+d"}}
            }
        }
    ]
}

TYPE_TAG = {'key': 'TOUCHE', 'url': 'WEB', 'http': 'HTTP', 'run': 'CMD',
            'alert': 'FIP'}


class DeckApp(App):
    name = 'deck'
    title = 'DECK'
    show_footer = False

    def __init__(self, daemon):
        super().__init__(daemon)
        self.cfg = {'pages': []}
        self.view = 0                  # page courante du deck
        self._mtime = None
        self._load(first=True)

    # ---------- config (recharge a chaud) ----------

    def _load(self, first=False):
        try:
            m = os.path.getmtime(CFG)
        except OSError:
            if first:
                self._save(SAMPLE)
            return
        if m == self._mtime:
            return
        self._mtime = m
        try:
            with open(CFG, encoding='utf-8') as f:
                cfg = json.load(f)
            if not cfg.get('pages'):
                raise ValueError('aucune page')
            self.cfg = cfg
            self.view = min(self.view, len(cfg['pages']) - 1)
        except Exception as e:
            print('[deck] config illisible:', e, flush=True)
            if first:
                self.cfg = SAMPLE

    def _save(self, cfg):
        try:
            with open(CFG, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print('[deck] ecriture config:', e, flush=True)

    # ---------- actions ----------

    def _do(self, btn):
        act = (btn or {}).get('action') or {}
        kind = act.get('type')
        try:
            if kind == 'key':
                import keysend
                keysend.tap(act['combo'])
            elif kind == 'url':
                os.startfile(act['url'])              # navigateur par defaut
            elif kind == 'http':
                import requests
                r = requests.request(act.get('method', 'POST'), act['url'],
                                     headers=act.get('headers'),
                                     json=act.get('json'), timeout=8)
                self.alert(f'HTTP {r.status_code}')
            elif kind == 'run':
                out = subprocess.run(act['cmd'], capture_output=True, timeout=60,
                                     creationflags=CREATE_NO_WINDOW)
                self.alert('OK' if out.returncode == 0
                           else 'echec code ' + str(out.returncode))
            elif kind == 'alert':
                self.alert(act.get('message', '!'))
            else:
                self.alert('type inconnu: ' + str(kind))
        except Exception as e:
            self.alert(str(e)[:30])

    # ---------- evenements ----------

    def on_event(self, name, pressed):
        if not pressed:
            return
        npages = len(self.cfg['pages'])
        if name == 'right_cw' and npages > 1:
            self.view = (self.view + 1) % npages
        elif name == 'right_ccw' and npages > 1:
            self.view = (self.view - 1) % npages
        elif name in ('S1', 'S2', 'S3', 'S4', 'S5', 'S6'):
            btns = self.cfg['pages'][self.view].get('buttons', {})
            btn = btns.get(name)
            if not btn:
                return
            threading.Thread(target=self._do, args=(btn,), daemon=True).start()

    def tick(self):
        super().tick()
        self._load()                    # mtime -> recharge a chaud

    def http_post(self, payload):
        """POST /app/deck {"page": N}"""
        if 'page' in payload:
            self.view = max(0, min(len(self.cfg['pages']) - 1, int(payload['page'])))
            self.d._redraw_now()
        return {'ok': True, 'view': self.view}

    # ---------- rendu ----------

    def render(self, d):
        F_S = fipui.font(20)
        F_XS = fipui.font(14)
        npages = len(self.cfg['pages'])
        page = self.cfg['pages'][self.view]
        btns = page.get('buttons', {})
        for r in range(6):
            y = r * 40
            yc = y + 20
            if r:
                d.line((0, y, 319, y), fill=(32, 38, 56))
            btn = btns.get(f'S{r + 1}')
            if btn:
                tag = TYPE_TAG.get((btn.get('action') or {}).get('type'), '?')
                d.text((16, yc - 8), str(btn.get('label', '?'))[:24], font=F_S,
                       fill=fipui.FG, anchor='lm')
                d.text((16, yc + 11), tag, font=F_XS, fill=fipui.DIM, anchor='lm')
            else:
                d.text((16, yc), '-', font=F_S, fill=(70, 75, 92), anchor='lm')
            d.text((306, yc), f'S{r + 1}', font=F_XS, fill=(90, 95, 110),
                   anchor='rm')
        if npages > 1:                       # repere compact, sous le tag de la 6e rangee
            tag = f"{page.get('title', 'DECK')} · {self.view + 1}/{npages}"
            while tag and d.textlength(tag, font=fipui.font(11)) > 170:
                tag = tag[1:]
            d.text((316, 233), tag, font=fipui.font(11), fill=(120, 120, 130),
                   anchor='rm')
