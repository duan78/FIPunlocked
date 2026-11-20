"""
fipd v3 — Flight Instrument Panel : daemon generique via la pile officielle.

Backend : DirectOutput.dll (client pipes) -> DirectOutputService (service Logitech).
Pages = APPLICATIONS fipui (fipui.py) : rendu, boutons/molettes, LEDs persistantes,
alertes. Pages integrees : Horloge, Systeme, Reseau, TEST boutons, Lecture, Demo.

Par defaut les touches ne pilotent QUE le FIP ; racourcis clavier opt-in via
fipd_bindings.json (recharge a chaud) ou POST /bindings.

APPS TIERCES (hot-load) : chaque fichier apps/*.py expose des sous-classes de
fipui.App (avec name != 'app', pas 'abstract') ; chargees au demarrage et via
POST /apps/reload. Une app peut definir http_get()/http_post(payload) -> dict,
exposes via GET/POST /app/<name>.

API http://127.0.0.1:8199 :
  POST /screen (PNG/JPG/BMP/WEBP ou ?url=)   affiche une image
  POST /video (fichier)                      joue une video (bouton S1 = stop)
  POST /ui                                   revient a l'interface integree
  GET/POST /bindings                         racourcis clavier (JSON)
  GET /keys                                  noms de touches reconnus
  POST /led {"index":1..8,"on":bool}         LED (ou {"scan":true})
  POST /apps/reload                          recharge apps/ a chaud
  GET/POST /app/<name>                       endpoint propre a une app
  GET /buttons (SSE) /events /
"""
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fipui
from fipui import App, draw_header, draw_menu, draw_bar

PORT = 8199
FW_BASE = 1               # ids firmware des pages : 1..N
SCROLL_STEP = 44          # px par cran de molette (page Lecture)
DO_DIR = r'C:\Program Files\Logitech\DirectOutput'
APPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apps')

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
PAGE_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, wt.BOOL, ctypes.c_void_p)

VOID_P = ctypes.c_void_p
do.DirectOutput_Enumerate.argtypes = [ENUM_CB, VOID_P]
do.DirectOutput_RegisterSoftButtonCallback.argtypes = [VOID_P, BTN_CB, VOID_P]
do.DirectOutput_RegisterPageCallback.argtypes = [VOID_P, PAGE_CB, VOID_P]
do.DirectOutput_AddPage.argtypes = [VOID_P, wt.DWORD, wt.LPCWSTR, wt.DWORD]
do.DirectOutput_RemovePage.argtypes = [VOID_P, wt.DWORD]
do.DirectOutput_SetImage.argtypes = [VOID_P, wt.DWORD, wt.DWORD, wt.DWORD, VOID_P]
do.DirectOutput_SetLed.argtypes = [VOID_P, wt.DWORD, wt.DWORD, wt.DWORD]

import keysend

SOFTBUTTONS = {0x01: 'select', 0x02: 'right_cw', 0x04: 'right_ccw', 0x08: 'left_ccw',
               0x10: 'left_cw', 0x20: 'S1', 0x40: 'S2', 0x80: 'S3',
               0x100: 'S4', 0x200: 'S5', 0x400: 'S6'}
LED_OF = {'S1': 1, 'S2': 2, 'S3': 3, 'S4': 4, 'S5': 5, 'S6': 6}   # LEDs 7-8 : libres

BINDINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fipd_bindings.json')

DEFAULT_BINDINGS = {
    'paging': True,
    'led_feedback': True,          # LED du bouton allumee pendant l'appui
    'bindings': {n: None for n in SOFTBUTTONS.values()},
    'pages': {},
}


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


class Bindings:
    """Raccourcis clavier des boutons (fipd_bindings.json, recharge a chaud)."""

    def __init__(self):
        self.cfg = dict(DEFAULT_BINDINGS)
        self._mtime = None
        self.reload()

    def reload(self):
        try:
            m = os.path.getmtime(BINDINGS_FILE)
        except OSError:
            if not os.path.exists(BINDINGS_FILE):
                self.save()
            return
        if m == self._mtime:
            return
        self._mtime = m
        try:
            with open(BINDINGS_FILE, encoding='utf-8') as f:
                self.cfg = json.load(f)
        except Exception as e:
            print('bindings illisibles:', e, flush=True)

    def save(self):
        with open(BINDINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        try:
            self._mtime = os.path.getmtime(BINDINGS_FILE)
        except OSError:
            pass

    def active(self, mode, page):
        self.reload()
        merged = dict(self.cfg.get('bindings', {}))
        if mode == 'ui':
            merged.update(self.cfg.get('pages', {}).get(str(page), {}))
        return merged

    def dispatch(self, edges, mode, page):
        binds = self.active(mode, page)
        for name, pressed in edges.items():
            combo = binds.get(name)
            if not combo:
                continue
            try:
                parts = keysend.parse_combo(combo)
            except KeyError as e:
                print(f'bindings: touche inconnue {e} pour {name}', flush=True)
                continue
            try:
                keysend.combo_up(parts) if not pressed else keysend.combo_down(parts)
            except Exception as e:
                print('keysend erreur:', e, flush=True)

    def paging_enabled(self, mode, page):
        binds = self.active(mode, page)
        return (self.cfg.get('paging', True)
                and not binds.get('left_cw') and not binds.get('left_ccw'))


# ============================ APPLICATIONS integrees ============================

class ClockApp(App):
    name = 'clock'
    title = 'HORLOGE'

    def render(self, d):
        d.text((160, 90), time.strftime('%H:%M:%S'), font=F_BIG,
               fill=(255, 255, 255), anchor='mm')
        d.text((160, 140), time.strftime('%A %d %B %Y'), font=F_MED,
               fill=(180, 220, 255), anchor='mm')


class SystemApp(App):
    name = 'system'
    title = 'SYSTEME'

    def render(self, d):
        import psutil
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        d.text((15, 12), 'SYSTEME', font=F_MED, fill=(255, 210, 80))
        d.text((15, 45), 'CPU  {:5.1f} %'.format(cpu), font=F_MED, fill=(255, 255, 255))
        draw_bar(d, 15, 75, 305, 87, cpu / 100, (80, 200, 120))
        d.text((15, 105), 'RAM  {:5.1f} %'.format(mem), font=F_MED, fill=(255, 255, 255))
        draw_bar(d, 15, 135, 305, 147, mem / 100, (200, 120, 80))
        disk = psutil.disk_usage('C:').percent
        d.text((15, 165), 'Disque C: {:.0f} %'.format(disk), font=F_SML,
               fill=(200, 200, 200))


class NetworkApp(App):
    name = 'network'
    title = 'RESEAU'

    def render(self, d):
        import psutil
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


class TesterApp(App):
    """Testeur : S1-S6 + select/molettes, retour visuel immediat."""
    name = 'tester'
    title = 'TEST BOUTONS'

    def __init__(self, daemon):
        super().__init__(daemon)
        self.state = 0              # masque boutons courant

    def on_event(self, name, pressed):
        bit = next((b for b, n in SOFTBUTTONS.items() if n == name), None)
        if bit is None:
            return
        if pressed:
            self.state |= bit
        else:
            self.state &= ~bit

    def render(self, d):
        d.text((160, 12), 'TEST BOUTONS', font=F_MED, fill=(255, 210, 80), anchor='mm')
        boxes = [(0x20, 'S1'), (0x40, 'S2'), (0x80, 'S3'),
                 (0x100, 'S4'), (0x200, 'S5'), (0x400, 'S6')]
        bw, bh, gap, x0, y0 = 92, 52, 12, 10, 30
        for i, (bit, name) in enumerate(boxes):
            x = x0 + (i % 3) * (bw + gap)
            y = y0 + (i // 3) * (bh + 10)
            on = self.state & bit
            if on:
                d.rectangle((x, y, x + bw, y + bh), fill=(70, 200, 120),
                            outline=(180, 255, 200))
                d.text((x + bw // 2, y + bh // 2), name, font=F_MED,
                       fill=(10, 30, 16), anchor='mm')
            else:
                d.rectangle((x, y, x + bw, y + bh), outline=(90, 95, 110))
                d.text((x + bw // 2, y + bh // 2), name, font=F_MED,
                       fill=(150, 155, 170), anchor='mm')
        mini = [(0x01, 'SEL'), (0x10, 'L>'), (0x08, '<L'), (0x02, 'R>'), (0x04, '<R')]
        x = 12
        for bit, lab in mini:
            on = self.state & bit
            d.rectangle((x, 158, x + 52, 178),
                        fill=(60, 90, 160) if on else None,
                        outline=(90, 95, 110) if not on else (150, 180, 255))
            d.text((x + 26, 168), lab, font=F_SML,
                   fill=(235, 240, 255) if on else (140, 145, 160), anchor='mm')
            x += 60


class ReaderApp(App):
    """Long texte defilant a la molette droite."""
    name = 'reader'
    title = 'JOURNAL'
    show_footer = False

    LONG_TEXT = (
        "FIPd — journal du driver maison\n"
        "\n"
        "04/11/2026 — Naissance du projet. Le Flight Instrument Panel de Saitek "
        "(VID_06A3, PID_A2AE) croupissait dans un tiroir : son logiciel officiel "
        "ne servait qu'aux simulateurs de vol. Objectif : en faire un ecran 320x240 "
        "generique, avec ses 6 boutons et ses 2 molettes utilisables.\n"
        "\n"
        "Premiere soiree : retro-ingenerie du protocole USB. L'interface bulk du "
        "driver accepte des requetes a en-tete de 44 octets en big-endian : "
        "ServerID, Flags, Longueur, RequestID... L'image est du BGR 320x240x24 "
        "brut, 230400 octets. Premier carre rouge affiche a 23h12 — victoire.\n"
        "\n"
        "Mais l'ecran retombe sur sa page d'accueil Saitek toutes les 4 secondes "
        "sans rafraichissement : le firmware exige un keepalive. Un thread de "
        "reaffichage regle le probleme.\n"
        "\n"
        "05/11/2026 — La chasse aux boutons. Le HID de l'interface MI_00 reste "
        "muet : le firmware n'emmet rien tant qu'une page n'est pas active chez "
        "l'hote. SPAD.neXt, lui, y arrive — on le lance, on le tue, on prend le "
        "relais. Le daemon v2 marche, mais depend de SPAD. Inacceptable.\n"
        "\n"
        "19/11/2026 — La verite eclate. DirectOutput.dll n'est qu'un client de "
        "pipes nommes vers DirectOutputService, un service systeme qui heberge "
        "le vrai plugin device. Une DLL proxy compilee avec gcc s'intercale dans "
        "le service et loggue sa sequence d'init : Create, callbacks, Activate. "
        "Aucun upload de firmware — la theorie du serveur embarque etait fausse.\n"
        "\n"
        "Le client Python officiel marche enfin : Initialize, Enumerate, "
        "RegisterSoftButtonCallback, AddPage(ACTIVE), SetImage. Les 6 boutons "
        "respondent, les molettes pulsent cran par cran. Le daemon v3 ne depend "
        "plus de rien de payant.\n"
        "\n"
        "20/11/2026 — Les LEDs se revelent : SetLed devait simplement viser la "
        "page COURANTE. Huit LED pilotables, une par bouton. Les applications "
        "fipui naissent : chaque page possede son rendu, ses evenements, ses "
        "LEDs persistantes et ses alertes.\n"
        "\n"
        "Prochaines etapes : capturer les octets exacts de l'activation de page "
        "pour s'affranchir totalement de la pile Logitech sous Windows, puis "
        "porter le pilote pur sous Linux (libusb + hidraw). L'aventure continue."
    )

    def __init__(self, daemon):
        super().__init__(daemon)
        self.scroll = 0
        self._img = None

    def on_enter(self):
        self.apply_leds()

    def on_event(self, name, pressed):
        if not pressed:
            return
        if name == 'right_cw':
            self.scroll = min(self.scroll + SCROLL_STEP, self.text_max())
        elif name == 'right_ccw':
            self.scroll = max(self.scroll - SCROLL_STEP, 0)

    def text_img(self):
        import textwrap
        from PIL import Image, ImageDraw
        if self._img is None:
            lines = []
            for para in self.LONG_TEXT.split('\n'):
                lines += textwrap.wrap(para, width=44) or ['']
            h = 14 + len(lines) * 17 + 20
            img = Image.new('RGB', (320, h), (12, 14, 20))
            d = ImageDraw.Draw(img)
            y = 8
            for ln in lines:
                d.text((10, y), ln, font=F_SML, fill=(222, 226, 235))
                y += 17
            self._img = img
        return self._img

    def text_max(self):
        return max(0, self.text_img().height - 212)

    def render(self, d):
        pass          # ReaderApp utilise render_full() (scroll = paste + crop)

    def render_full(self, img):
        """Rendu complet (necessite paste, pas seulement ImageDraw)."""
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, 319, 19), fill=(24, 28, 40))
        d.text((8, 10), 'JOURNAL', font=F_MED, fill=(255, 210, 80), anchor='lm')
        d.text((312, 10), 'molette droite', font=F_SML, fill=(130, 135, 150),
               anchor='rm')
        win = self.text_img().crop((0, self.scroll, 320, self.scroll + 212))
        img.paste(win, (0, 20))
        mx = self.text_max()
        if mx > 0:
            d.rectangle((314, 24, 318, 228), outline=(70, 75, 90))
            th = max(8, int(204 * 212 / (mx + 212)))
            ty = 24 + int((204 - th) * self.scroll / mx)
            d.rectangle((315, ty, 317, ty + th), fill=(90, 160, 230))


class SettingsDemoApp(App):
    """Demo fipui : liste DEFILANTE viewport-relative.
    8 items, 6 lignes visibles x 40 px alignees sur les boutons physiques.
    Molette droite = defilement ; boutons S1-S6 et LEDs 1-6 agissent sur les
    lignes VISIBLES (les etiquettes S1..S6 suivent le defilement).
    Items 'Redemarrer' et 'Diagnostique' = actions (alerte)."""
    name = 'demo'
    title = 'DEMO FIPUI'
    show_footer = False

    ITEMS = ['WiFi', 'Bluetooth', 'Mode sombre', 'Notifications', 'Veille auto',
             'Redemarrer', 'Mode avion', 'Diagnostique']
    ACTIONS = {5: 'Redemarrage !', 7: 'Diag OK'}
    ROW_H = 40                        # 6 x 40 = 240 px : pleine hauteur
    VISIBLE = 6

    def __init__(self, daemon):
        super().__init__(daemon)
        self.view = 0                 # premiere ligne visible
        self.states = {0: True, 1: False, 2: True, 3: True, 4: False, 6: False}

    @property
    def max_view(self):
        return max(0, len(self.ITEMS) - self.VISIBLE)

    def sync_leds(self):
        """LEDs 1-6 = etats des lignes VISIBLES (elles suivent le defilement)."""
        for r in range(self.VISIBLE):
            self.leds[r + 1] = self.states.get(self.view + r, False)

    def on_enter(self):
        self.sync_leds()
        self.apply_leds()

    def _act(self, item):
        if item in self.ACTIONS:
            self.alert(self.ACTIONS[item])
        else:
            self.states[item] = not self.states[item]
            self.sync_leds()
            self.apply_leds()

    def on_event(self, name, pressed):
        if not pressed:
            return
        if name == 'right_cw':
            self.view = min(self.view + 1, self.max_view)
            self.sync_leds()
            self.apply_leds()
        elif name == 'right_ccw':
            self.view = max(self.view - 1, 0)
            self.sync_leds()
            self.apply_leds()
        elif name.startswith('S') and name[1:].isdigit():
            r = int(name[1:]) - 1                 # S1..S6 -> ligne visible r
            item = self.view + r
            if item < len(self.ITEMS):
                self._act(item)

    def render(self, d):
        for r in range(self.VISIBLE):
            item = self.view + r
            if item >= len(self.ITEMS):
                break
            y = r * self.ROW_H
            yc = y + self.ROW_H // 2
            if r:                                  # separateur fin
                d.line((0, y, 316, y), fill=(32, 38, 56))
            state = self.states.get(item)
            if state is not None:                  # pastille on/off a droite
                d.ellipse((294, yc - 10, 310, yc + 10),
                          fill=fipui.GREEN if state else (45, 50, 64),
                          outline=(160, 220, 180) if state else (80, 85, 100))
            elif item in self.ACTIONS:             # marqueur action
                d.polygon([(296, yc - 8), (308, yc), (296, yc + 8)],
                          fill=fipui.ACCENT)
            d.text((18, yc), self.ITEMS[item], font=F_MED,
                   fill=fipui.FG, anchor='lm')
            d.text((280, yc), f'S{r+1}', font=F_SML, fill=(90, 95, 110),
                   anchor='rm')
        # mini barre de defilement sur le bord droit
        n = len(self.ITEMS)
        if n > self.VISIBLE:
            ty0, ty1 = 6, 234
            th = (ty1 - ty0) * self.VISIBLE // n
            ty = ty0 + (ty1 - ty0 - th) * self.view // self.max_view
            d.rectangle((317, ty0, 319, ty1), fill=(30, 35, 50))
            d.rectangle((317, ty, 319, ty + th), fill=fipui.BLUE)


class GameApp(App):
    """REFLEX — mini-jeu : des taupes apparaissent dans les 6 lignes (LED miroir),
    tape le bouton correspondant avant la fin du chrono.
    N'importe quel bouton S1-S6 = lancer/rejouer · S1-S6 = taper en jeu
    · molette droite = niveau + · molette gauche = niveau - (capturée par le
    jeu, uniquement sur cette page ; les boutons centraux restent la pagination,
    gérés par le firmware). NB : le FIP n'a PAS de bouton select physique."""
    name = 'game'
    title = 'REFLEX'
    show_footer = False
    capture_left_knob = True

    LIVES = 3
    ROW_H = 40                       # 6 x 40 : lignes alignees sur les boutons

    def __init__(self, daemon):
        super().__init__(daemon)
        self.state = 'idle'          # idle | run | over
        self.score = 0
        self.best = 0
        self.lives = self.LIVES
        self.level = 1
        self.mole = None             # (slot, t0, t1)
        self.next_at = 0.0
        self._stop = threading.Event()
        self._thread = None

    # ---------- boucle de jeu (thread propre, dessins via _redraw_now) ----------

    def on_enter(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def on_leave(self):
        self._stop.set()
        for i in range(1, 7):
            self.d.set_led(i, False)

    def _window_ms(self):
        return max(350, 1100 - 120 * self.level)

    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            if self.state == 'run':
                if self.mole and now > self.mole[2]:
                    self._miss()
                elif not self.mole and now > self.next_at:
                    self._spawn(now)
            self._stop.wait(0.05)

    def _spawn(self, now):
        import random
        slot = random.randrange(6)
        w = self._window_ms() / 1000.0
        self.mole = (slot, now, now + w)
        self.d.set_led(slot + 1, True)
        self.d._redraw_now()

    def _miss(self):
        self.mole = None
        self.lives -= 1
        self.next_at = time.time() + 0.4
        if self.lives <= 0:
            self._game_over()
        self.d._redraw_now()

    def _game_over(self):
        self.state = 'over'
        self.best = max(self.best, self.score)
        self.mole = None
        for i in range(1, 7):
            self.d.set_led(i, False)
        self.alert(f'Score {self.score}')

    # ---------- controles ----------

    def on_event(self, name, pressed):
        if not pressed:
            return
        if name == 'right_cw':
            self.level = min(9, self.level + 1)
        elif name == 'left_cw':
            self.level = max(1, self.level - 1)
        elif name.startswith('S') and name[1:].isdigit():
            if self.state != 'run':           # idle/over : n'importe quel bouton lance
                self.score = 0
                self.lives = self.LIVES
                self.mole = None
                self.next_at = time.time() + 0.8
                self.state = 'run'
                self.d._redraw_now()
                return
            if not self.mole:
                return
            slot, t0, t1 = self.mole
            if int(name[1:]) - 1 == slot:
                remaining = max(0.0, t1 - time.time())
                self.score += 10 * self.level + int(10 * remaining / (self._window_ms() / 1000.0))
                self.d.set_led(slot + 1, False)
                self.mole = None
                self.next_at = time.time() + 0.3
            else:
                self._miss()
            self.d._redraw_now()

    # ---------- rendu ----------

    def render(self, d):
        for r in range(6):
            y = r * self.ROW_H
            yc = y + self.ROW_H // 2
            if r:
                d.line((0, y, 191, y), fill=(32, 38, 56))
            active = (self.state == 'run' and self.mole and self.mole[0] == r)
            if active:
                d.rectangle((6, y + 4, 186, y + 36), fill=fipui.BLUE)
                d.text((96, yc), f'TAPE S{r+1} !', font=F_MED,
                       fill=(255, 255, 255), anchor='mm')
            else:
                d.text((14, yc), f'S{r+1}', font=F_SML, fill=(90, 95, 110),
                       anchor='lm')
                d.ellipse((178, yc - 4, 186, yc + 4),
                          fill=(45, 50, 64) if self.state != 'run' else (60, 70, 95))
        # panneau de droite : score / vies / niveau / consigne
        d.rectangle((198, 8, 312, 150), fill=(24, 28, 40), outline=(45, 52, 66))
        d.text((255, 26), 'REFLEX', font=F_MED, fill=fipui.ACCENT, anchor='mm')
        d.text((255, 56), str(self.score), font=F_BIG, fill=fipui.FG, anchor='mm')
        d.text((255, 88), 'VIES ' + str(max(0, self.lives)), font=F_MED,
               fill=fipui.GREEN if self.lives > 1 else fipui.RED, anchor='mm')
        d.text((255, 112), f'NIVEAU {self.level}', font=F_MED, fill=fipui.FG,
               anchor='mm')
        d.text((255, 136), f'RECORD {self.best}', font=F_SML, fill=fipui.DIM,
               anchor='mm')
        if self.state == 'idle':
            d.text((255, 176), 'un bouton = jouer', font=F_MED, fill=fipui.FG,
                   anchor='mm')
            d.text((255, 200), 'molettes = niveau', font=F_SML, fill=fipui.DIM,
                   anchor='mm')
            d.text((255, 218), 'S1-S6 = taper', font=F_SML, fill=fipui.DIM,
                   anchor='mm')
        elif self.state == 'over':
            d.text((255, 190), 'PERDU !', font=F_MED, fill=fipui.RED, anchor='mm')
            d.text((255, 214), 'un bouton = rejouer', font=F_SML, fill=fipui.DIM,
                   anchor='mm')


# ================================ Daemon ================================

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
        self.bindings = Bindings()
        self._prev_btn = 0
        self._page_cb = None      # reference PageChange (GC)
        self._draw_evt = threading.Event()   # redraw asynchrone (jamais de SetImage
                                             # depuis un callback du service = reentrance)
        self._gap_timer = None    # detection de l'ecran home Saitek (a sauter)
        self._own_nav = False     # nos propres activations (ne pas detecter de trou)
        self.chrome_tabs = []     # dernier flux de l'extension (POST /app/fenetres)
        self.chrome_tabs_t = 0.0
        self.tab_cmd = None       # commande en attente pour l'extension
        self.tab_gen = 0
        self.apps = self._builtin_apps() + self._discover_apps()

    def queue_tab_cmd(self, cmd):
        """Commande a executer par l'extension Chrome (protocole cursor)."""
        self.tab_cmd = cmd
        self.tab_gen += 1

    def _npages(self):
        return len(self.apps)

    # ---------- applications (integrees + tierces hot-chargees) ----------

    def _builtin_apps(self):
        return [ClockApp(self), SystemApp(self), NetworkApp(self),
                TesterApp(self), ReaderApp(self), SettingsDemoApp(self),
                GameApp(self)]

    def _discover_apps(self):
        """Importe apps/*.py (hors _*.py) et instancie les fipui.App trouvees
        (name != 'app', pas 'abstract'). Un fichier peut definir plusieurs apps."""
        import importlib.util
        found = []
        if not os.path.isdir(APPS_DIR):
            return found
        for fn in sorted(os.listdir(APPS_DIR)):
            if not fn.endswith('.py') or fn.startswith('_'):
                continue
            modname = f'_fipapp_{fn[:-3]}'
            try:
                spec = importlib.util.spec_from_file_location(
                    modname, os.path.join(APPS_DIR, fn))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as e:
                print(f'[apps] {fn}: import echoue: {e}', flush=True)
                continue
            n = 0
            for cls in vars(mod).values():
                if (isinstance(cls, type) and issubclass(cls, App)
                        and cls.__module__ == modname
                        and getattr(cls, 'name', 'app') != 'app'
                        and not getattr(cls, 'abstract', False)):
                    try:
                        inst = cls(self)
                        inst._external = True
                        found.append(inst)
                        n += 1
                    except Exception as e:
                        print(f'[apps] {fn}:{cls.__name__}: instanciation: {e}',
                              flush=True)
            if n:
                print(f'[apps] {fn}: {n} app(s) chargee(s)', flush=True)
        return found

    def reload_apps(self):
        """Reconstruit la liste complete (integrees + apps/) et re-enregistre
        les pages firmware. L'etat des apps existantes est perdu (reinstanciees)."""
        if self.mode != 'ui':
            return {'error': 'mode ' + self.mode + ' actif : POST /ui avant'}
        import sys
        # les modules partages des apps (charte...) doivent suivre le reload :
        # sinon python sert la version en cache et les apps plantent sur les
        # nouveaux symboles. fipui/keysend restent (classes/interfaces vives).
        sys.modules.pop('charte', None)
        self._own_nav = True
        for a in self.apps:                      # arreter les threads des apps
            try:                                 # (fuite : instances rechargees)
                if getattr(a, '_stop', None) is not None:
                    a._stop.set()
            except Exception:
                pass
        try:
            self.apps[self.page].on_leave()
        except Exception:
            pass
        with self.lock:
            for i in range(self._npages()):
                try:
                    do.DirectOutput_RemovePage(self.hdev, FW_BASE + i)
                except OSError:
                    pass
            time.sleep(0.2)
            self.apps = self._builtin_apps() + self._discover_apps()
            self.page = min(self.page, self._npages() - 1)
            for i in range(self._npages()):
                try:
                    do.DirectOutput_AddPage(self.hdev, FW_BASE + i,
                                            f'fipd-{self.apps[i].name}',
                                            1 if i == self.page else 0)
                except OSError:
                    pass
        self._own_nav = False
        self._enter_app(self.page)
        self._redraw_now()
        return {'ok': True, 'pages': self._npages(),
                'apps': [a.title for a in self.apps]}

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
        # PageChange : boutons page ↑/↓ du firmware (entre les molettes)
        self._page_cb = PAGE_CB(self._on_page)
        do.DirectOutput_RegisterPageCallback(self.hdev, self._page_cb, None)
        # purge defensive : pages d'une session precedente mal terminee
        self._own_nav = True                  # nos Add/Remove ne doivent pas armer
        for i in range(self._npages()):       # la detection d'ecran home
            try:
                do.DirectOutput_RemovePage(self.hdev, FW_BASE + i)
            except OSError:
                pass
        time.sleep(0.3)
        hrs = []
        for i in range(self._npages()):
            hrs.append(do.DirectOutput_AddPage(self.hdev, FW_BASE + i,
                                               f'fipd-{self.apps[i].name}',
                                               1 if i == 0 else 0))  # 1 = ACTIVE
        # E_PAGENOTACTIVE : seul un dessin sur la page COURANTE est accepte.
        # Toutes les pages sont enregistrees (boutons ↑/↓), puis la 1re est courante.
        self._activate(0)
        self.set_led(7, False)                # LEDs fixes (7-8) eteintes par defaut :
        self.set_led(8, False)                # pilotables librement via POST /led
        self._refresh_page(0)
        time.sleep(0.5)                       # laisser fuiter les callbacks d'init
        self._own_nav = False
        self.status = (f'OK (device 0x{self.hdev:x}, {self._npages()} pages: ' +
                       ' '.join(f'{h & 0xFFFFFFFF:08X}' for h in hrs) + ')')
        threading.Thread(target=self._keepalive, daemon=True).start()
        return True

    # ---------- pages firmware ----------

    def _refresh_page(self, page, img=None):
        """Envoie le rendu de la page vers son slot firmware (avec retry).
        NE JAMAIS appeler depuis un callback du service (reentrance RPC).
        Retourne True si l'image est partie."""
        if not self.hdev:
            return False
        from PIL import Image
        img = img if img is not None else self.render_page(page)
        img = img.convert('RGB').resize((320, 240)).transpose(Image.FLIP_TOP_BOTTOM)
        buf = ctypes.create_string_buffer(img.tobytes(), 320 * 240 * 3)
        for attempt in range(3):
            if self.mode == 'ui' and page != self.page:
                return False          # page perimee (changement pendant le rendu)
            try:
                with self.lock:
                    do.DirectOutput_SetImage(self.hdev, FW_BASE + page, 0,
                                             320 * 240 * 3, buf)
                    if page == self.page:
                        self._last_raw = buf
                return True
            except OSError as e:
                if attempt == 2:
                    msg = (f'refresh page {page+1} echoue: {e.winerror & 0xFFFFFFFF:08X}'
                           if e.winerror else f'refresh page {page+1} echoue: {e}')
                    print(msg, flush=True)
                time.sleep(0.25)
        return False

    def _activate(self, page):
        """Rend la page COURANTE cote firmware. Empirique (probe_pages2) :
        seul le dessin sur la page courante est accepte (FF040001 sinon) ;
        AddPage rend la nouvelle page courante ; re-activer une page existante
        exige RemovePage + AddPage. Le tampon image est perdu -> redraw apres."""
        if not self.hdev:
            return None
        fw = FW_BASE + page
        try:
            do.DirectOutput_RemovePage(self.hdev, fw)
        except OSError:
            pass                      # page inconnue : premier enregistrement
        time.sleep(0.03)
        try:
            do.DirectOutput_AddPage(self.hdev, fw, f'fipd-{self.apps[page].name}', 1)
            return 0
        except OSError as e:
            print(f'activate page {page+1} echoue: '
                  f'{e.winerror & 0xFFFFFFFF:08X}' if e.winerror else
                  f'activate page {page+1} echoue', flush=True)
            return -1

    def _switch_page(self, new):
        """Navigation molette gauche : bornee (pas de bouclage).
        Appelle depuis le callback boutons -> programmation asynchrone."""
        new = max(0, min(self._npages() - 1, new))
        if new == self.page:
            return
        self.apps[self.page].on_leave()
        self.page = new
        self._pending_activate = new          # le thread d'affichage activera + dessinera
        self._draw_evt.set()

    def _enter_app(self, idx):
        """Cycle d'entree : LEDs persistantes re-appliquees (tampons perdus),
        LEDs non revendiquees (7-8 en particulier) eteintes."""
        try:
            self.apps[idx].on_enter()
            self.apps[idx].restore_leds()
        except Exception as e:
            print(f'on_enter({idx}): {e}', flush=True)

    def _cancel_gap(self):
        if self._gap_timer is not None:
            self._gap_timer.cancel()
            self._gap_timer = None

    def _arm_gap(self, idx):
        """Apres la desactivation de la page idx sans activation qui suit :
        le firmware est tombe sur son ecran home (Saitek) -> on saute au
        prolongement du cycle (home est sur l'axe 1<->N)."""
        self._cancel_gap()
        if idx not in (0, self._npages() - 1):
            return

        def skip_home():
            target = self._npages() - 1 if idx == 0 else 0
            self._gap_timer = None
            if self._own_nav or self.page != idx:
                return
            self.apps[idx].on_leave()
            self.page = target
            self._pending_activate = target
            self._draw_evt.set()
            print(f'[page] home Saitek saute -> page {target + 1}', flush=True)

        self._gap_timer = threading.Timer(0.05, skip_home)
        self._gap_timer.daemon = True
        self._gap_timer.start()

    def _on_page(self, h, fwpage, active, ctx):
        """Callback firmware : boutons page ↑/↓ (entre les molettes).
        AUCUN appel DirectOutput ici (reentrance) : on programme le redraw."""
        idx = fwpage - FW_BASE
        if fwpage < 0x80000000:              # ids plausibles seulement (pas les -1)
            print(f'[page] t={time.time():.3f} fw={fwpage} active={bool(active)} '
                  f'idx={idx}', flush=True)
        if not (0 <= idx < self._npages()):
            return
        if active:
            self._cancel_gap()
            if idx != self.page:
                self.apps[self.page].on_leave()
                self.page = idx
            self.mode = 'ui'                  # les boutons ↑/↓ reviennent au dashboard
            self._enter_app(idx)
            self._draw_evt.set()              # le thread d'affichage dessinera
            ev = {'t': time.time(), 'state': 0, 'names': ['page_updown'], 'page': idx}
            self.events.append(ev)
            self.events = self.events[-200:]
            for q in list(self.sse):
                try:
                    q.append(ev)
                except Exception:
                    self.sse.remove(q)
        elif not self._own_nav:
            self._arm_gap(idx)

    # ---------- boutons ----------

    def _on_btn(self, h, buttons, ctx):
        names = [n for b, n in SOFTBUTTONS.items() if buttons & b]
        ev = {'t': time.time(), 'state': buttons, 'names': names, 'page': self.page}
        self.events.append(ev)
        self.events = self.events[-200:]
        for q in list(self.sse):
            try:
                q.append(ev)
            except Exception:
                self.sse.remove(q)

        # arret video : S1 en mode video (prioritaire sur les bindings)
        if self.mode == 'video' and buttons & 0x20 and not self._prev_btn & 0x20:
            self.video_stop.set()

        # edges : appuis / relachements
        pressed = buttons & ~self._prev_btn
        released = self._prev_btn & ~buttons
        self._prev_btn = buttons
        edges = {}
        for bit, n in SOFTBUTTONS.items():
            if pressed & bit:
                edges[n] = True
            elif released & bit:
                edges[n] = False
        if edges:
            self.bindings.dispatch(edges, self.mode, self.page)
            # retour lumineux : LED du bouton pendant l'appui (local au FIP)
            app = self.apps[self.page]
            fb = app.led_feedback
            if fb is None:
                fb = self.bindings.cfg.get('led_feedback', True)
            if fb:
                for name, was_pressed in edges.items():
                    led = LED_OF.get(name)
                    if led:
                        self.set_led(led, was_pressed)

        # actions internes (pages du FIP) — par defaut les touches restent locales
        if self.mode == 'ui':
            binds = self.bindings.active(self.mode, self.page)
            if (self.bindings.paging_enabled(self.mode, self.page)
                    and not getattr(self.apps[self.page], 'capture_left_knob', False)):
                if edges.get('left_ccw'):
                    self._switch_page(self.page + 1)      # borne, pas de bouclage
                elif edges.get('left_cw'):
                    self._switch_page(self.page - 1)
            # evenements remis a l'app courante (apres pagination eventuelle)
            app = self.apps[self.page]
            for name, was_pressed in edges.items():
                if binds.get(name):
                    continue          # controle binde clavier : l'app cede
                try:
                    app.on_event(name, was_pressed)
                except Exception as e:
                    print(f'on_event({name}): {e}', flush=True)
            self._redraw_now()        # retour visuel immediat

    # ---------- LEDs ----------

    def set_led(self, index, on):
        """LED d'un bouton lateral, sur la page COURANTE (regle E_PAGENOTACTIVE).
        Retourne le hr pour diagnostic."""
        try:
            return do.DirectOutput_SetLed(self.hdev, FW_BASE + self.page,
                                          index, 1 if on else 0) & 0xFFFFFFFF
        except OSError as e:
            return (e.winerror or 0) & 0xFFFFFFFF

    def scan_leds(self):
        """Balaie indices 0..12 on/off (400 ms) : l'utilisateur guette une lumiere."""
        results = {}
        for idx in list(range(0, 13)):
            results[f'led{idx}_on'] = self.set_led(idx, True)
            time.sleep(0.4)
            self.set_led(idx, False)
            time.sleep(0.2)
        return results

    # ---------- affichage ----------

    def render_page(self, page=None):
        """Rendu PIL d'une page (fond + app + alerte + footer).
        Une app qui definit render_full(img) gere son rendu complet (paste
        d'images : scroll, pochette, flux video...)."""
        from PIL import Image, ImageDraw
        page = self.page if page is None else page
        app = self.apps[page]
        img = Image.new('RGB', (320, 240), fipui.BG)
        d = ImageDraw.Draw(img)
        try:
            if hasattr(app, 'render_full'):
                app.render_full(img)
                d = ImageDraw.Draw(img)      # l'alerte passe au-dessus du rendu
                app.draw_alert(d, F_MED)
            else:
                app.render(d)
                app.draw_alert(d, F_MED)
                if app.show_footer:
                    d.text((160, 228), 'page {}/{}  (molette gauche)'.format(
                        page + 1, self._npages()), font=F_SML,
                        fill=(120, 120, 130), anchor='mm')
        except Exception as e:
            # filet de securite : JAMAIS d'ecran noir — carte d'erreur
            import traceback
            print(f'render({app.name}): {e}', flush=True)
            traceback.print_exc()
            d = ImageDraw.Draw(img)
            d.rectangle((0, 0, 319, 239), fill=(30, 12, 12))
            d.rectangle((4, 4, 315, 34), fill=(120, 30, 25))
            d.text((160, 19), 'ERREUR PAGE ' + app.title[:12], font=F_MED,
                   fill=(255, 255, 255), anchor='mm')
            d.text((160, 70), app.name, font=F_MED, fill=(255, 180, 170),
                   anchor='mm')
            msg = str(e).split('\n')[0]
            while msg and d.textlength(msg, font=F_SML) > 300:
                msg = msg[:-1]
            d.text((160, 100), msg or '(erreur inconnue)', font=F_SML,
                   fill=(255, 200, 190), anchor='mm')
            d.text((160, 140), 'corrigez puis POST /apps/reload',
                   font=F_SML, fill=(200, 150, 140), anchor='mm')
            app.draw_alert(d, F_MED)
        return img

    def _send(self, img):
        """Modes image/video : ecrit sur la page firmware 1."""
        from PIL import Image
        img = img.convert('RGB').resize((320, 240)).transpose(Image.FLIP_TOP_BOTTOM)
        buf = ctypes.create_string_buffer(img.tobytes(), 320 * 240 * 3)
        with self.lock:                      # callback boutons + keepalive serrialises
            self._last_raw = buf
            do.DirectOutput_SetImage(self.hdev, FW_BASE, 0, 320 * 240 * 3, buf)

    def _redraw_now(self):
        """Programme un redraw immediat (thread d'affichage)."""
        self._draw_evt.set()

    def show_image(self, img, mode='image'):
        self.mode = mode
        self._send(img)

    def _keepalive(self):
        """Thread d'affichage unique : tous les appels DirectOutput de dessin passent
        ici (les callbacks du service ne font que programmer via _draw_evt)."""
        while True:
            app = self.apps[self.page] if self.mode == 'ui' else None
            fast = bool(app and app.alert_active())
            self._draw_evt.wait(0.3 if fast else 2.0)
            self._draw_evt.clear()
            if not self.hdev:
                continue
            try:
                if self.mode == 'ui':
                    app = self.apps[self.page]
                    try:
                        app.tick()
                    except Exception as e:
                        print(f'tick({app.name}): {e}', flush=True)
                    pend = getattr(self, '_pending_activate', None)
                    if pend is not None:
                        self._pending_activate = None
                        self._own_nav = True
                        try:
                            self._activate(pend)
                        finally:
                            self._own_nav = False
                        self._enter_app(pend)    # tampons perdus : LEDs a re-appliquer
                    ok = self._refresh_page(self.page)
                    # AUTO-GUERISON : le firmware est tombe sur son ecran home
                    # (ou la page a perdu son etat actif) -> E_PAGENOTACTIVE a
                    # repetition. On re-active la page courante et on redessine.
                    if ok:
                        self._fails = 0
                    else:
                        self._fails = getattr(self, '_fails', 0) + 1
                        if self._fails >= 2:
                            print(f'[auto-guerison] re-activation page '
                                  f'{self.page + 1} (echecs: {self._fails})',
                                  flush=True)
                            self._own_nav = True
                            try:
                                self._activate(self.page)
                            finally:
                                self._own_nav = False
                            self._enter_app(self.page)
                            self._refresh_page(self.page)
                            self._fails = 0
                elif getattr(self, '_last_raw', None):
                    with self.lock:
                        do.DirectOutput_SetImage(self.hdev, FW_BASE, 0,
                                                 320 * 240 * 3, self._last_raw)
            except Exception as e:
                import traceback
                print('keepalive:', e, flush=True)
                traceback.print_exc()

    # ---------- video ----------

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
        elif self.path == '/bindings':
            D.bindings.reload()
            self._json(D.bindings.cfg)
        elif self.path == '/keys':
            self._json({'keys': keysend.key_names(),
                        'exemple': 'ctrl+shift+s, volumemute, f5, alt+tab'})
        elif self.path == '/apps':
            D.bindings.reload()
            self._json({'apps': [{'index': i, 'name': a.name, 'title': a.title,
                                  'external': getattr(a, '_external', False)}
                                 for i, a in enumerate(D.apps)],
                        'page': D.page, 'mode': D.mode})
        elif self.path.startswith('/app/'):
            import inspect
            from urllib.parse import urlsplit, parse_qs
            u = urlsplit(self.path)
            name = u.path[len('/app/'):].strip('/')
            app = next((a for a in D.apps if a.name == name), None)
            if app is None:
                return self._json({'error': f'app inconnue: {name}'}, 404)
            h = getattr(app, 'http_get', None)
            if h is None:
                return self._json({'error': f'{name} n expose pas http_get'}, 405)
            try:
                if len(inspect.signature(h).parameters) >= 2:
                    return self._json(h(parse_qs(u.query)))
                return self._json(h())
            except Exception as e:
                return self._json({'error': str(e)}, 500)
        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            body = ('<html><body style="font-family:sans-serif;background:#111;color:#eee">'
                    '<h2>FIPd v3</h2><p>Status : ' + D.status + '</p><p>Mode : ' + D.mode +
                    '</p><p>Page : ' + str(D.page + 1) + '/' + str(D._npages()) + '</p>'
                    '<p>POST /screen (image) - POST /video - POST /ui - POST /led - '
                    'GET /buttons (SSE) - GET /apps</p></body></html>').encode()
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
            if self.path == '/led':
                dd = json.loads(body or b'{}')
                if dd.get('scan'):
                    return self._json({'scan': D.scan_leds()})
                idx = int(dd.get('index', 0))
                on = bool(dd.get('on', True))
                hr = D.set_led(idx, on)
                return self._json({'ok': hr == 0, 'index': idx, 'on': on,
                                   'hr': f'{hr:08X}', 'page': D.page})
            if self.path == '/page':
                dd = json.loads(body or b'{}')
                D.mode = 'ui'
                D._switch_page(max(0, min(D._npages() - 1, int(dd.get('page', 0)))))
                return self._json({'ok': True, 'page': D.page})
            if self.path == '/apps/reload':
                return self._json(D.reload_apps())
            if self.path.startswith('/app/'):
                name = self.path[len('/app/'):].split('?')[0].strip('/')
                app = next((a for a in D.apps if a.name == name), None)
                if app is None:
                    return self._json({'error': f'app inconnue: {name}'}, 404)
                h = getattr(app, 'http_post', None)
                if h is None:
                    return self._json({'error': f'{name} n expose pas http_post'}, 405)
                try:
                    payload = json.loads(body or b'{}')
                    out = h(payload)
                    # etat partage : les onglets Chrome interessent plusieurs
                    # apps (fenetres, contexte)
                    if name == 'fenetres' and isinstance(payload.get('tabs'),
                                                         list):
                        D.chrome_tabs = payload['tabs']
                        D.chrome_tabs_t = time.time()
                    return self._json(out)
                except Exception as e:
                    return self._json({'error': str(e)}, 500)
            if self.path == '/bindings':
                try:
                    cfg = json.loads(body)
                except Exception as e:
                    return self._json({'error': f'json invalide: {e}'}, 400)
                for scope in [cfg.get('bindings', {})] + list(cfg.get('pages', {}).values()):
                    for name, combo in scope.items():
                        if combo and name not in SOFTBUTTONS.values():
                            return self._json({'error': f'bouton inconnu: {name}'}, 400)
                        if combo:
                            try:
                                keysend.parse_combo(combo)
                            except Exception as e:
                                return self._json({'error': f'{name}: {e}'}, 400)
                D.bindings.cfg = cfg
                D.bindings.save()
                return self._json({'ok': True, 'bindings': D.bindings.cfg})
            return self._json({'error': 'inconnu'}, 404)
        except Exception as e:
            return self._json({'error': str(e)}, 500)


def main():
    import atexit
    import sys
    print('fipd v3 — connexion a la pile officielle (service DirectOutputService)...', flush=True)
    if not D.start():
        print('ECHEC :', D.status, flush=True)
        print("Demarrez le service : sc start DirectOutputService  (admin)", flush=True)
        sys.exit(1)
    atexit.register(lambda: (setattr(D, 'hdev', None),
                             do.DirectOutput_Deinitialize()))   # libere nos pages
    print(f'Pret : http://127.0.0.1:8199 — {D.status}', flush=True)
    print(f'{D._npages()} applications : ' + ', '.join(a.title for a in D.apps), flush=True)
    print('Molette gauche = page. S1 = stop video. GET /apps pour la liste.', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()


if __name__ == '__main__':
    main()
