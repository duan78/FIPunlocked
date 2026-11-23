"""
fipd v3 — Flight Instrument Panel : daemon generique via la pile officielle.

Backend : DirectOutput.dll (client pipes) -> DirectOutputService (service Logitech).
Pages = APPLICATIONS fipui (fipui.py) : rendu, boutons/molettes, LEDs persistantes,
alertes. Pages integrees : Horloge, Systeme, Reseau, TEST boutons, Lecture, Demo.

Par defaut les touches ne pilotent QUE le FIP ; racourcis clavier opt-in via
fipd_bindings.json (recharge a chaud) ou POST /bindings.

APPS TIERCES (hot-load) : chaque fichier apps/*.py expose des sous-classes de
fipui.App (avec name != 'app', pas 'abstract') ; chargees au demarrage et via
POST /apps/reload (seuls les fichiers modifies sont reconstruits : les autres
apps gardent leur etat). Une app peut definir http_get()/http_post(payload) -> dict,
exposes via GET/POST /app/<name>.

API http://127.0.0.1:8199 (strictement locale : un navigateur web ne peut pas
l'appeler — refus de toute requete portant un Origin ou un Host non loopback) :
  POST /screen (PNG/JPG/BMP/WEBP ou ?url=http...)  affiche une image
  POST /video (fichier X-Filename)            joue une video (S1 = stop)
  POST /ui                                   revient a l'interface integree
  GET/POST /bindings                         racourcis clavier (JSON)
  GET /keys                                  noms de touches reconnus
  POST /led {"index":1..8,"on":bool}         LED (ou {"scan":true})
  GET /apps · POST /page {"page":N}          liste des pages / change de page
  POST /apps/reload [{"app":nom}|{"all":true}] recharge apps/ a chaud
  POST /device/reset                         refait la session DirectOutput (FIP rebranche)
  POST /exit                                 arret propre (libere les pages)
  GET/POST /app/<name>                       endpoint propre a une app
  GET /buttons (SSE) /events /

Importer ce module n'a AUCUN effet de bord : la DLL DirectOutput est chargee par
Daemon.start() et l'instance D est creee par main() (tests, gen_screens).
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import html
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import fipui
from fipui import App, draw_bar

PORT = 8199
FW_BASE = 1               # ids firmware des pages : 1..N
SCROLL_STEP = 44          # px par cran de molette (page Lecture)
DO_DIR = r'C:\Program Files\Logitech\DirectOutput'
HERE = os.path.dirname(os.path.abspath(__file__))
APPS_DIR = os.path.join(HERE, 'apps')
SERVICE = 'DirectOutputService'
MAX_UPLOAD = 32 * 1024 * 1024     # /screen et /video : corps ou telechargement
RECOVER_AFTER = 3                 # auto-guerisons ratees (page inactive) avant re-initialisation
KEEPALIVE_S = 2.0                 # rafraichissement normal (le firmware rentre a l'idle en 2-3 s)
FAIL_WAIT_S = 0.5                 # cadence quand un rafraichissement echoue
RECOVER_QUICK_S = 2.0             # apres une perte de session : tentatives toutes les 2 s...
RECOVER_QUICK_FOR_S = 30.0        # ... pendant 30 s, puis
RECOVER_EVERY_S = 10.0            # ... toutes les 10 s
REINIT_TIMEOUT_S = 40.0           # Initialize doit rendre la main : sinon la pile est bloquee
E_PAGENOTACTIVE = 0xFF040001      # firmware : la page n'est pas courante (auto-guerison)
SESSION_ERRORS = (0x8000FFFF, 0x80070006)   # E_UNEXPECTED, E_HANDLE : service arrete ou redemarre
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '[::1]')

SOFTBUTTONS = {0x01: 'select', 0x02: 'right_cw', 0x04: 'right_ccw', 0x08: 'left_ccw',
               0x10: 'left_cw', 0x20: 'S1', 0x40: 'S2', 0x80: 'S3',
               0x100: 'S4', 0x200: 'S5', 0x400: 'S6'}
LED_OF = {'S1': 1, 'S2': 2, 'S3': 3, 'S4': 4, 'S5': 5, 'S6': 6}   # LEDs 7-8 : libres

BINDINGS_FILE = os.path.join(HERE, 'fipd_bindings.json')

DEFAULT_BINDINGS = {
    'paging': True,
    'led_feedback': True,          # LED du bouton allumee pendant l'appui
    'bindings': {n: None for n in SOFTBUTTONS.values()},
    'pages': {},
}


def _stat(path):
    """Empreinte du CONTENU d'un fichier (sha1), ou None s'il est absent.
    Un fichier modifie se detecte par son contenu : ni la date (granularite
    de l'horloge systeme) ni la taille (edition de meme longueur) ne
    distinguent surement deux enregistrements rapproches."""
    try:
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


class _FreshSource(importlib.machinery.SourceFileLoader):
    """Chargeur qui compile TOUJOURS la source lue, sans .pyc. Python tient un
    .pyc pour valide sur la date en SECONDES et la taille de la source : une
    edition de meme longueur enregistree dans la meme seconde que le
    chargement precedent relancerait l'ancien code (defaut revele par
    l'integration continue, masque en local par des tests lances avec -B)."""

    def get_code(self, fullname):
        return self.source_to_code(self.get_data(self.path), self.path)


def _module_from_source(name, path):
    """Module `name` execute depuis le contenu ACTUEL de `path` (cf. _FreshSource),
    deja place dans sys.modules. Si l'execution echoue (syntaxe, import...),
    l'ancien module reste en place et l'exception est relayee."""
    spec = importlib.util.spec_from_file_location(name, path, loader=_FreshSource(name, path))
    mod = importlib.util.module_from_spec(spec)
    old = sys.modules.get(name)
    sys.modules[name] = mod              # tracebacks et repr lisibles
    try:
        spec.loader.exec_module(mod)
    except Exception:
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old
        raise
    return mod


_SELF_STAT = _stat(os.path.join(HERE, os.path.basename(__file__)))


# ============================ pile officielle ============================

ENUM_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
BTN_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p)
PAGE_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, wt.BOOL, ctypes.c_void_p)
VOID_P = ctypes.c_void_p

do = None       # DirectOutput.dll, chargee par Daemon.start() (import sans device)


def _load_do():
    """Charge DirectOutput.dll et fixe ses prototypes (une seule fois)."""
    global do
    if do is not None:
        return do
    os.chdir(DO_DIR)
    lib = ctypes.WinDLL(os.path.join(DO_DIR, 'DirectOutput.dll'))
    for n in ('DirectOutput_Initialize', 'DirectOutput_Deinitialize',
              'DirectOutput_RegisterDeviceCallback', 'DirectOutput_Enumerate',
              'DirectOutput_RegisterPageCallback', 'DirectOutput_RegisterSoftButtonCallback',
              'DirectOutput_AddPage', 'DirectOutput_RemovePage', 'DirectOutput_SetLed',
              'DirectOutput_SetImage', 'DirectOutput_SetImageFromFile', 'DirectOutput_SetProfile'):
        getattr(lib, n).restype = ctypes.HRESULT
    lib.DirectOutput_Enumerate.argtypes = [ENUM_CB, VOID_P]
    lib.DirectOutput_RegisterSoftButtonCallback.argtypes = [VOID_P, BTN_CB, VOID_P]
    lib.DirectOutput_RegisterPageCallback.argtypes = [VOID_P, PAGE_CB, VOID_P]
    lib.DirectOutput_AddPage.argtypes = [VOID_P, wt.DWORD, wt.LPCWSTR, wt.DWORD]
    lib.DirectOutput_RemovePage.argtypes = [VOID_P, wt.DWORD]
    lib.DirectOutput_SetImage.argtypes = [VOID_P, wt.DWORD, wt.DWORD, wt.DWORD, VOID_P]
    lib.DirectOutput_SetLed.argtypes = [VOID_P, wt.DWORD, wt.DWORD, wt.DWORD]
    do = lib
    return lib


def service_state(name=SERVICE):
    """Etat d'un service Windows ('RUNNING', 'STOPPED', 'START_PENDING'...),
    None s'il est introuvable. Sans ce pre-check, DirectOutput_Initialize
    bloque SANS FIN quand le service ne tourne pas."""
    states = {1: 'STOPPED', 2: 'START_PENDING', 3: 'STOP_PENDING', 4: 'RUNNING',
              5: 'CONTINUE_PENDING', 6: 'PAUSE_PENDING', 7: 'PAUSED'}
    try:
        adv = ctypes.WinDLL('advapi32')

        class SERVICE_STATUS(ctypes.Structure):
            _fields_ = [('dwServiceType', wt.DWORD), ('dwCurrentState', wt.DWORD),
                        ('dwControlsAccepted', wt.DWORD), ('dwWin32ExitCode', wt.DWORD),
                        ('dwServiceSpecificExitCode', wt.DWORD),
                        ('dwCheckPoint', wt.DWORD), ('dwWaitHint', wt.DWORD)]

        adv.OpenSCManagerA.restype = ctypes.c_void_p
        adv.OpenSCManagerA.argtypes = [ctypes.c_char_p, ctypes.c_char_p, wt.DWORD]
        adv.OpenServiceA.restype = ctypes.c_void_p
        adv.OpenServiceA.argtypes = [ctypes.c_void_p, ctypes.c_char_p, wt.DWORD]
        adv.QueryServiceStatus.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(SERVICE_STATUS)]
        adv.CloseServiceHandle.argtypes = [ctypes.c_void_p]
        scm = adv.OpenSCManagerA(None, None, 0x0001)      # SC_MANAGER_CONNECT
        if not scm:
            return None
        try:
            svc = adv.OpenServiceA(scm, name.encode(), 0x0004)   # SERVICE_QUERY_STATUS
            if not svc:
                return None
            try:
                st = SERVICE_STATUS()
                if not adv.QueryServiceStatus(svc, ctypes.byref(st)):
                    return None
                return states.get(st.dwCurrentState, str(st.dwCurrentState))
            finally:
                adv.CloseServiceHandle(svc)
        finally:
            adv.CloseServiceHandle(scm)
    except Exception:
        return None


def _download(url):
    """Image distante pour /screen?url= (http/https uniquement, taille bornee)."""
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'fipd/3'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = r.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise RuntimeError('image trop volumineuse')
    return data


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
        self.cfg = json.loads(json.dumps(DEFAULT_BINDINGS))   # copie profonde
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
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError('racine JSON attendue : objet')
            self.cfg = cfg
        except Exception as e:
            print('bindings illisibles:', e, flush=True)   # l'ancienne config reste

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
                keysend.combo_up(parts) if not pressed else keysend.combo_down(parts)
            except Exception as e:
                print(f'bindings: {name}={combo}: {e}', flush=True)

    def paging_enabled(self, mode, page):
        binds = self.active(mode, page)
        return (self.cfg.get('paging', True)
                and not binds.get('left_cw') and not binds.get('left_ccw'))


import keysend  # noqa: E402  (apres les prototypes : aucun materiel requis)

# ============================ APPLICATIONS integrees ============================
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


# ================================ Daemon ================================


class Daemon:
    def __init__(self, with_apps=True):
        self.mode = 'ui'
        self.page = 0
        self.events = []
        self.sse = []
        self.lock = threading.RLock()        # appels DirectOutput de dessin
        self.nav_lock = threading.Lock()     # compteur de navigation propre
        self._own_depth = 0
        self.hdev = None
        self.do = None
        self._inited = False                 # DirectOutput_Initialize passe
        self._lost = False                   # une session valide a ete perdue
        self.status = 'demarrage'
        self.video_stop = threading.Event()
        self._stopping = threading.Event()     # arret du thread d'affichage (shutdown)
        self._ka_thread = None
        self._last_ok = None                   # date de la derniere image acceptee
        self._last_err = 0                     # code de la derniere erreur DirectOutput
        self._lost_since = None                # date (monotone) du debut de la perte de session
        self.bindings = Bindings()
        self._prev_btn = 0
        self._page_cb = None      # reference PageChange (GC)
        self._btn_cb = None       # reference SoftButton (GC)
        self._draw_evt = threading.Event()   # redraw asynchrone (jamais de SetImage
                                             # depuis un callback du service = reentrance)
        self._gap_timer = None    # detection de l'ecran home Saitek (a sauter)
        self._entered = None      # app deja entree (on_enter unique par sejour)
        self._entering = None     # page en cours d'activation par le thread d'affichage
        self._pending_activate = None
        self._fails = 0           # rafraichissements ratees d'affilee
        self._dead = 0            # auto-guerisons ratees d'affilee
        self._retry_at = 0.0
        self._last_wait_msg = 0.0
        self._reattaching = False
        self._stuck = False       # re-initialisation bloquee : redemarrage necessaire
        self.recoveries = 0
        self._overlay_from = None  # page d'origine des modes image/video
        self.apps = self._builtin_apps() + (self._discover_apps() if with_apps else [])
        self._shared = {n: _stat(p) for n, p in self._shared_modules().items()}

    def _npages(self):
        return len(self.apps)

    # ---------- navigation propre (compteur : plusieurs threads peuvent imbriquer) ----------

    @property
    def _own_nav(self):
        return self._own_depth > 0

    @contextmanager
    def _own(self):
        """Nos propres Remove/AddPage ne doivent pas armer la detection d'ecran home."""
        with self.nav_lock:
            self._own_depth += 1
        try:
            yield
        finally:
            with self.nav_lock:
                self._own_depth -= 1

    # ---------- applications (integrees + tierces hot-chargees) ----------

    def _builtin_apps(self):
        return [ClockApp(self), SystemApp(self), NetworkApp(self),
                TesterApp(self), ReaderApp(self), SettingsDemoApp(self)]

    @staticmethod
    def _app_files():
        """{fichier: chemin} des apps tierces (apps/*.py hors _*.py), triees."""
        if not os.path.isdir(APPS_DIR):
            return {}
        return {fn: os.path.join(APPS_DIR, fn) for fn in sorted(os.listdir(APPS_DIR))
                if fn.endswith('.py') and not fn.startswith('_')}

    @staticmethod
    def _shared_modules():
        """{nom: chemin} des modules charges qui vivent dans le dossier du daemon
        (charte, uiatabs...) — hors le daemon lui-meme et les apps tierces."""
        out = {}
        for name, mod in list(sys.modules.items()):
            if name in ('fipd3', '__main__') or name.startswith('_fipapp_'):
                continue
            fn = getattr(mod, '__file__', None) or ''
            if os.path.isdir(fn) or not fn.endswith('.py'):
                continue
            fn = os.path.abspath(fn)
            if os.path.dirname(fn) == HERE:
                out[name] = fn
        return out

    def _load_app_file(self, fn, path):
        """Importe un fichier d'app -> (instances, erreurs). Un fichier peut
        definir plusieurs apps. Les instances portent _file/_stat (reload cible).
        L'empreinte est prise AVANT l'import : une edition qui survient pendant
        l'import reste a detecter au prochain reload (l'inverse la cacherait)."""
        modname = f'_fipapp_{fn[:-3]}'
        fp = _stat(path)
        try:
            mod = _module_from_source(modname, path)
        except Exception as e:
            msg = f'[apps] {fn}: import echoue: {e}'
            print(msg, flush=True)
            return [], [msg]
        found, errors = [], []
        for cls in list(vars(mod).values()):
            if (isinstance(cls, type) and issubclass(cls, App)
                    and cls.__module__ == modname
                    and getattr(cls, 'name', 'app') != 'app'
                    and not getattr(cls, 'abstract', False)):
                try:
                    inst = cls(self)
                    inst._external = True
                    inst._file = fn
                    inst._stat = fp
                    found.append(inst)
                except Exception as e:
                    msg = f'[apps] {fn}:{cls.__name__}: instanciation: {e}'
                    print(msg, flush=True)
                    errors.append(msg)
        if found:
            print(f'[apps] {fn}: {len(found)} app(s) chargee(s)', flush=True)
        return found, errors

    def _discover_apps(self):
        """Importe apps/*.py et instancie les fipui.App trouvees."""
        found = []
        for fn, path in self._app_files().items():
            insts, _ = self._load_app_file(fn, path)
            found += insts
        return found

    def reload_apps(self, only=None):
        """Recharge apps/ A CHAUD.
        only=None : seuls les fichiers MODIFIES sont re-importes ; les autres
        apps gardent leur instance et leur etat (radio, partie, caches...).
        only='*' : tout est reconstruit ; only='<app ou fichier>' : celle-la seule.
        Un module partage de fipd/ (charte...) modifie => tout est reconstruit ;
        uiatabs / fipui / keysend / fipd3 modifies => redemarrage requis (signale).
        Un fichier qui casse : l'ancienne version qui marchait est conservee."""
        if self.mode != 'ui':
            return {'error': 'mode ' + self.mode + ' actif : POST /ui avant'}
        # --- modules partages : modifies depuis le chargement ?
        restart, errors = [], []
        rebuild = False
        for name, path in self._shared_modules().items():
            now = _stat(path)
            if now == self._shared.get(name):
                continue
            self._shared[name] = now
            if name in ('uiatabs', 'fipui', 'keysend'):
                restart.append(name)
                continue
            try:
                _module_from_source(name, path)     # les apps le re-importent
            except Exception as e:                  # casse : l'ancien module reste
                errors.append(f'[shared] {name}: import echoue: {e}')
                print(errors[-1], flush=True)
                continue
            rebuild = True
        if _stat(os.path.abspath(__file__)) != _SELF_STAT:
            restart.append('fipd3')
        rebuild = rebuild or only == '*'

        # --- decide, fichier par fichier : conserver ou re-importer
        old_ext = [a for a in self.apps if getattr(a, '_external', False)]
        old_names = [a.name for a in self.apps]
        shown = self.apps[self.page]             # instance affichee AVANT le reload
        new_ext, reloaded = [], []
        for fn, path in self._app_files().items():
            olds = [a for a in old_ext if getattr(a, '_file', None) == fn]
            wanted = (only is not None and only != '*'
                      and only in (fn, fn[:-3]) + tuple(a.name for a in olds))
            same = (bool(olds) and not rebuild and not wanted
                    and all(a._stat == _stat(path) for a in olds))
            if same:
                new_ext += olds
                continue
            insts, errs = self._load_app_file(fn, path)
            if insts:
                new_ext += insts
                reloaded.append(fn)
            else:
                errors += errs
                if olds:                         # fichier casse : l'ancienne version,
                    for a in olds:               # qui marchait, reste en service
                        a._stat = _stat(path)
                    new_ext += olds
        for a in old_ext:                        # instances remplacees ou supprimees :
            if a not in new_ext:                 # on arrete leurs threads
                a._stop.set()
                a._shown.clear()
        self.apps = [a for a in self.apps if not getattr(a, '_external', False)] + new_ext

        # --- pages firmware : ne bougent que si la liste des pages change
        reregistered = False
        if [a.name for a in self.apps] != old_names:
            self.page = min(self.page, self._npages() - 1)
            if self.hdev:
                reregistered = True
                with self._own():
                    with self.lock:
                        for i in range(len(old_names)):
                            try:
                                self.do.DirectOutput_RemovePage(self.hdev, FW_BASE + i)
                            except OSError:
                                pass
                        time.sleep(0.2)
                        for i in range(self._npages()):
                            try:
                                self.do.DirectOutput_AddPage(
                                    self.hdev, FW_BASE + i, f'fipd-{self.apps[i].name}',
                                    1 if i == self.page else 0)
                            except OSError:
                                pass
        if shown in self.apps:                   # l'app affichee garde la main
            self.page = self.apps.index(shown)
        if self.mode == 'ui' and (reregistered
                                  or self._entered is not self.apps[self.page]):
            self._leave(shown)
            self._enter_app(self.page, force=True)
        self._redraw_now()
        return {'ok': True, 'pages': self._npages(),
                'reloaded': reloaded,
                'kept': sorted(a.name for a in new_ext
                               if not any(a._file == f for f in reloaded)),
                'errors': errors, 'restart_required': restart,
                'apps': [a.title for a in self.apps]}

    # ---------- demarrage pile officielle ----------

    def _init_stack(self):
        """Initialize (une fois) -> Enumerate -> callbacks -> pages.
        Retourne True SEULEMENT si une image a ete ACCEPTEE par le peripherique ;
        False sinon (absent, muet, service pas encore pret) : le daemon reste
        lance et reessaie.

        Constate sur le materiel (23/11) : apres un redemarrage du service,
        l'enumeration retourne l'ANCIEN peripherique — un handle mort, E_HANDLE
        a chaque appel — EN PLUS du nouveau. On essaie donc chaque handle, du
        plus recent au plus ancien, et on garde le premier qui accepte une
        page et une image."""
        lib = self.do or _load_do()
        self.do = lib
        if not self._inited:
            try:
                hr = lib.DirectOutput_Initialize('fipd')
            except OSError as e:        # E_FAIL juste apres un redemarrage du service
                hr = (e.winerror or 0) & 0xFFFFFFFF
            if hr != 0:
                self.status = (f'Initialize hr=0x{hr & 0xFFFFFFFF:08X} '
                               f'(service {SERVICE} pas encore pret ?)')
                return False
            self._inited = True

        found = []

        @ENUM_CB
        def on_enum(h, ctx):
            found.append(h)

        lib.DirectOutput_Enumerate(on_enum, None)
        if not found:
            self.hdev = None
            self.status = 'aucun peripherique DirectOutput (FIP debranche ?)'
            self._lost = False                 # rien a reconstruire : il n'y a jamais eu de session
            return False
        if len(found) > 1:
            print(f'[device] {len(found)} peripheriques enumeres : on garde '
                  'celui qui accepte une image', flush=True)
        old = self.hdev
        order = [h for h in found if h != old][::-1] + ([old] if old in found else [])
        try:                                   # FIPD_DEVICE_INDEX : handle essaie en premier
            prefer = found[max(0, min(int(os.environ.get('FIPD_DEVICE_INDEX', 0)),
                                      len(found) - 1))]
            order.sort(key=lambda h: h != prefer)
        except ValueError:
            pass
        dead = []
        for h in order:
            self.hdev = h
            try:
                self._setup_device(lib, h)
            except OSError:
                dead.append(h)
                continue
            if not self._refresh_page(self.page, force=True):
                dead.append(h)                 # rien n'y fait : handle mort
                continue
            self._fails = self._dead = 0
            self._lost = False
            self._lost_since = None
            self.status = (f'OK (device 0x{h:x}, {self._npages()} pages'
                           + (f', {len(dead)} handle(s) mort(s))' if dead else '') + ')')
            return True
        self.hdev = None
        self.status = ('peripherique muet : aucune image acceptee '
                       f'({len(found)} enumeres)')
        return False

    def _setup_device(self, lib, h):
        """Callbacks + pages sur le peripherique h, la page AFFICHEE etant
        courante (pas de flash d'horloge a la reprise). Leve OSError si h est
        un handle mort."""
        self._btn_cb = BTN_CB(self._on_btn)   # reference gardee : le service l'appellera
        lib.DirectOutput_RegisterSoftButtonCallback(h, self._btn_cb, None)
        # PageChange : boutons page ↑/↓ du firmware (entre les molettes)
        self._page_cb = PAGE_CB(self._on_page)
        lib.DirectOutput_RegisterPageCallback(h, self._page_cb, None)
        # purge defensive : pages d'une session precedente mal terminee
        with self._own():                     # nos Add/Remove ne doivent pas armer
            for i in range(self._npages()):   # la detection d'ecran home
                try:
                    lib.DirectOutput_RemovePage(h, FW_BASE + i)
                except OSError:
                    pass
            time.sleep(0.3)
            # E_PAGENOTACTIVE : seul un dessin sur la page COURANTE est accepte.
            # Toutes les pages sont enregistrees (boutons ↑/↓), la page
            # affichee etant courante.
            for i in range(self._npages()):
                lib.DirectOutput_AddPage(h, FW_BASE + i, f'fipd-{self.apps[i].name}',
                                         1 if i == self.page else 0)  # 1 = ACTIVE
            if self._activate(self.page) != 0:
                raise OSError(None, 'page courante refusee', None,
                              ctypes.c_long(E_PAGENOTACTIVE).value)
            self.set_led(7, False)            # LEDs fixes (7-8) eteintes par defaut :
            self.set_led(8, False)            # pilotables librement via POST /led
            time.sleep(0.5)                   # laisser fuiter les callbacks d'init
        self._enter_app(self.page, force=True)   # LEDs persistantes, pollers

    def start(self):
        """Connexion a la pile officielle + thread d'affichage. Sans peripherique,
        le daemon demarre quand meme et reessaie toutes les RECOVER_EVERY_S."""
        ok = self._init_stack()
        self._ka_thread = threading.Thread(target=self._keepalive, daemon=True,
                                           name='fipd-keepalive')
        self._ka_thread.start()
        return ok

    def shutdown(self):
        """Arret propre : arrete le thread d'affichage et libere nos pages
        (atexit / POST /exit)."""
        self._stopping.set()
        self.hdev = None
        lib, self.do = self.do, None
        self._inited = False
        if lib is not None:
            try:
                lib.DirectOutput_Deinitialize()
            except Exception as e:
                print('Deinitialize:', e, flush=True)

    # ---------- entree / sortie de page ----------

    def _leave(self, app):
        """Quitte une page (idempotent) : les pollers de la page s'endorment."""
        if self._entered is app:
            self._entered = None
        try:
            app._shown.clear()
            app._wake_polls()
        except Exception:
            pass
        try:
            app.on_leave()
        except Exception as e:
            print(f'on_leave({app.name}): {e}', flush=True)

    def _enter_app(self, idx, force=False):
        """Cycle d'entree : LEDs persistantes re-appliquees (tampons perdus),
        LEDs non revendiquees (7-8 en particulier) eteintes, pollers reveilles.
        Sans force : un seul on_enter par sejour sur la page (le callback
        PageChange ET le thread d'affichage appellent ici)."""
        app = self.apps[idx]
        if not force and self._entered is app:
            return
        self._entered = app
        try:
            app._shown.set()
            app._wake_polls()
        except Exception:
            pass
        try:
            app.on_enter()
            app.restore_leds()
        except Exception as e:
            print(f'on_enter({idx}): {e}', flush=True)

    # ---------- pages firmware ----------

    def _refresh_page(self, page, img=None, force=False):
        """Envoie le rendu de la page vers son slot firmware (avec retry).
        NE JAMAIS appeler depuis un callback du service (reentrance RPC).
        Retourne True si l'image est partie ; sinon self._last_err = code.
        force = verification d'un peripherique a l'initialisation (ignore
        l'etat « reconstruction en cours », un seul essai)."""
        if not self.hdev or (self._reattaching and not force):
            return False
        from PIL import Image
        img = img if img is not None else self.render_page(page)
        img = img.convert('RGB').resize((320, 240)).transpose(Image.FLIP_TOP_BOTTOM)
        buf = ctypes.create_string_buffer(img.tobytes(), 320 * 240 * 3)
        for attempt in range(3):
            if not force and self.mode == 'ui' and page != self.page:
                return False          # page perimee (changement pendant le rendu)
            if not self.hdev:
                return False          # session en cours de reconstruction
            try:
                with self.lock:
                    self.do.DirectOutput_SetImage(self.hdev, FW_BASE + page, 0,
                                                  320 * 240 * 3, buf)
                    if page == self.page:
                        self._last_raw = buf
                self._last_err = 0
                self._last_ok = time.time()
                return True
            except OSError as e:
                self._last_err = (e.winerror or 0) & 0xFFFFFFFF
                if force:
                    return False      # peripherique a verifier : un essai suffit
                if attempt == 2:
                    print(f'refresh page {page + 1} echoue: {self._last_err:08X}',
                          flush=True)
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
            self.do.DirectOutput_RemovePage(self.hdev, fw)
        except OSError:
            pass                      # page inconnue : premier enregistrement
        time.sleep(0.03)
        try:
            self.do.DirectOutput_AddPage(self.hdev, fw, f'fipd-{self.apps[page].name}', 1)
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
        self._leave(self.apps[self.page])
        self.page = new
        self._pending_activate = new          # le thread d'affichage activera + dessinera
        self._draw_evt.set()

    def _cancel_gap(self):
        if self._gap_timer is not None:
            self._gap_timer.cancel()
            self._gap_timer = None

    def _arm_gap(self, idx):
        """Apres la desactivation de la page idx sans activation qui suit :
        le firmware est tombe sur son ecran home (Saitek) -> on saute au
        prolongement du cycle (home est sur l'axe 1<->N)."""

        def skip_home():
            self._gap_timer = None
            if self._own_nav or self.page != idx:
                return
            target = self._npages() - 1 if idx == 0 else 0
            self._leave(self.apps[idx])
            self.page = target
            self._pending_activate = target
            self._draw_evt.set()
            print(f'[page] home Saitek saute -> page {target + 1}', flush=True)

        self._cancel_gap()
        if idx not in (0, self._npages() - 1):
            return
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
                self._leave(self.apps[self.page])
                self.page = idx
            if self.mode != 'ui':            # bouton ↑/↓ pendant une image/une video
                self.video_stop.set()
                self.mode = 'ui'
                self._overlay_from = None
            if self._entering != idx:        # sinon le thread d'affichage s'en charge
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
        else:
            if self._entered is self.apps[idx]:   # la page perd son activation :
                self._entered = None               # tampons perdus -> on_enter a refaire
            if not self._own_nav:
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
            # retour lumineux : LED du bouton pendant l'appui (local au FIP) ;
            # au relachement on rend a la LED l'etat que l'app lui donne
            app = self.apps[self.page]
            fb = app.led_feedback
            if fb is None:
                fb = self.bindings.cfg.get('led_feedback', True)
            if fb:
                for name, was_pressed in edges.items():
                    led = LED_OF.get(name)
                    if led:
                        self.set_led(led, was_pressed or app.leds.get(led, False))

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
        Retourne le hr pour diagnostic (0 sans peripherique)."""
        if not self.hdev:
            return 0
        try:
            return self.do.DirectOutput_SetLed(self.hdev, FW_BASE + self.page,
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
            try:
                app.draw_alert(d, F_MED)
            except Exception:
                pass
        return img

    def _send(self, img):
        """Modes image/video : ecrit sur la page firmware 1 (rendue courante par
        _begin_overlay — le firmware n'accepte un dessin que sur la page active)."""
        if not self.hdev:
            raise RuntimeError('aucun peripherique')
        from PIL import Image
        img = img.convert('RGB').resize((320, 240)).transpose(Image.FLIP_TOP_BOTTOM)
        buf = ctypes.create_string_buffer(img.tobytes(), 320 * 240 * 3)
        with self.lock:                      # callback boutons + keepalive serrialises
            self._last_raw = buf
            self.do.DirectOutput_SetImage(self.hdev, FW_BASE, 0, 320 * 240 * 3, buf)
        self._last_ok = time.time()

    def _redraw_now(self):
        """Programme un redraw immediat (thread d'affichage)."""
        self._draw_evt.set()

    def ok_age(self):
        """Secondes depuis la derniere image ACCEPTEE par le peripherique
        (None : jamais). > 5 s = l'affichage est en panne, en cours de
        recuperation — GET /apps l'expose, comme `recoveries`."""
        return None if self._last_ok is None else round(time.time() - self._last_ok, 1)

    # ---------- modes image / video ----------

    def _begin_overlay(self):
        """Le firmware n'accepte un dessin que sur la page COURANTE : la page 1
        devient courante et l'app affichee est quittee. La page d'origine est
        memorisee pour y revenir (_end_overlay)."""
        if self.mode != 'ui':
            return
        self._overlay_from = self.page
        self._leave(self.apps[self.page])
        self.page = 0
        with self._own():
            self._activate(0)

    def _end_overlay(self):
        """Retour a l'interface : la page d'origine est re-activee (et son app
        relancee) par le thread d'affichage."""
        was, self.mode = self.mode, 'ui'
        frm, self._overlay_from = self._overlay_from, None
        if was == 'ui' or frm is None:
            return
        self.page = frm
        self._pending_activate = frm
        self._draw_evt.set()

    def show_image(self, img, mode='image'):
        if not self.hdev:
            raise RuntimeError('aucun peripherique')
        self._begin_overlay()
        self.mode = mode
        try:
            self._send(img)
        except Exception:
            self._end_overlay()              # jamais un mode image sans image
            raise

    # ---------- reprise apres perte du peripherique ----------

    def _lose_session(self):
        """La session DirectOutput n'est plus utilisable : il faudra la reconstruire."""
        self._lost = True
        if self._lost_since is None:
            self._lost_since = time.monotonic()

    def _note_wait(self):
        """Annonce (au plus toutes les 60 s) qu'on attend le service ou le FIP."""
        if time.time() - self._last_wait_msg > 60:
            self._last_wait_msg = time.time()
            print(f'[recup] {self.status} — on reessaie', flush=True)

    def _recover(self):
        """Session perdue (service tombe, auto-guerisons ratees) ou peripherique
        jamais vu : re-initialise la pile. Premieres tentatives serrees
        (RECOVER_QUICK_S : le service revient en quelques secondes), puis
        RECOVER_EVERY_S si cela persiste."""
        if self._stuck or self._stopping.is_set():
            return
        now = time.monotonic()
        if now < self._retry_at:
            return
        quick = self._lost_since is not None and now - self._lost_since < RECOVER_QUICK_FOR_S
        self._retry_at = now + (RECOVER_QUICK_S if quick else RECOVER_EVERY_S)
        self._reattach(full=self._lost)

    def _reattach(self, full=True):
        """FIP debranche/rebranche, service redemarre : on refait toute la
        sequence (Deinitialize si une session a ete perdue) -> Initialize ->
        Enumerate -> callbacks -> pages.

        Uniquement service RUNNING : sans lui, DirectOutput_Initialize attend
        son pipe indefiniment. L'initialisation tourne dans un thread garde-fou
        (REINIT_TIMEOUT_S) : si elle bloque, on arrete d'insister.
        Retourne True seulement si une image a ete acceptee (_init_stack)."""
        if self._reattaching:
            return False
        self._reattaching = True
        try:
            st = service_state(SERVICE)
            if st != 'RUNNING':
                self.status = f'service {SERVICE} {st or "introuvable"} : en attente de son demarrage'
                self._note_wait()
                return False
            if full and self.do is not None and self._inited:
                print('[recup] session perdue -> re-initialisation de la pile '
                      'DirectOutput', flush=True)
                self.hdev = None
                try:
                    self.do.DirectOutput_Deinitialize()
                except Exception as e:
                    print('[recup] Deinitialize:', e, flush=True)
                self._inited = False
            done = threading.Event()
            result = []

            def go():
                try:
                    result.append(self._init_stack())
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    self.status = f're-initialisation echouee : {e}'
                    result.append(False)
                finally:
                    done.set()

            threading.Thread(target=go, daemon=True, name='fipd-reattach').start()
            if not done.wait(REINIT_TIMEOUT_S):
                self._stuck = True
                self.hdev = None
                self.status = 're-initialisation bloquee (service DirectOutput ?) — redemarrer fipd'
                print('[recup] re-initialisation bloquee : redemarrer fipd', flush=True)
                return False
            ok = bool(result and result[0])
            if ok:
                self.recoveries += 1
                self._lost_since = None
                self._last_wait_msg = 0.0
                print(f'[recup] session retablie (page {self.page + 1})', flush=True)
            else:
                self._note_wait()
            return ok
        finally:
            self._reattaching = False

    # ---------- boucle d'affichage ----------

    def _keepalive(self):
        """Thread d'affichage unique : tous les appels DirectOutput de dessin passent
        ici (les callbacks du service ne font que programmer via _draw_evt).
        Cadence : 2 s ; 0,3 s si une alerte clignote ; FAIL_WAIT_S des que
        l'auto-guerison echoue (peripherique perdu : on va jusqu'a la
        re-initialisation de la session sans attendre)."""
        while not self._stopping.is_set():
            app = self.apps[self.page] if self.mode == 'ui' else None
            fast = bool(app and app.alert_active())
            self._draw_evt.wait(0.3 if fast else (
                FAIL_WAIT_S if (self._dead or self._fails) else KEEPALIVE_S))
            self._draw_evt.clear()
            if self._stopping.is_set():
                break                          # arret : jamais de recuperation pendant la fermeture
            if not self.hdev:
                self._recover()              # FIP absent ou session perdue
                continue
            if self._reattaching:
                continue                     # session en reconstruction (POST /device/reset)
            try:
                if self.mode == 'ui':
                    app = self.apps[self.page]
                    try:
                        app.tick()
                    except Exception as e:
                        print(f'tick({app.name}): {e}', flush=True)
                    pend = self._pending_activate
                    if pend is not None:
                        self._pending_activate = None
                        self._entering = pend
                        try:
                            with self._own():
                                self._activate(pend)
                            self._enter_app(pend, force=True)   # tampons perdus : LEDs a re-appliquer
                        finally:
                            self._entering = None
                    ok = self._refresh_page(self.page)
                    # AUTO-GUERISON : le firmware est tombe sur son ecran home
                    # (ou la page a perdu son etat actif) -> E_PAGENOTACTIVE a
                    # repetition. On re-active la page courante et on redessine.
                    # Autre chose (service arrete/redemarre : E_UNEXPECTED,
                    # E_HANDLE) : re-activer la page ne sert a rien — constate
                    # sur le materiel — on reconstruit la session tout de suite.
                    if ok:
                        self._fails = self._dead = 0
                    else:
                        self._fails += 1
                        if self._fails >= 2:
                            self._fails = 0
                            if self._last_err in SESSION_ERRORS:
                                print(f'[recup] erreur de session {self._last_err:08X} '
                                      '(service arrete ou redemarre) -> reconstruction',
                                      flush=True)
                                self._lose_session()
                                self.hdev = None
                                self._recover()
                                continue
                            print(f'[auto-guerison] re-activation page '
                                  f'{self.page + 1} (echecs: {self._fails + 2})', flush=True)
                            # _entering : le callback PageChange ne doit pas
                            # refaire on_enter (ci-dessous, une seule fois)
                            self._entering = self.page
                            try:
                                with self._own():
                                    self._activate(self.page)
                                self._enter_app(self.page, force=True)
                            finally:
                                self._entering = None
                            if self._refresh_page(self.page):
                                self._dead = 0
                            else:
                                # rien n'y fait : peripherique debranche, page
                                # jamais acceptee -> on refait la session entiere
                                self._dead += 1
                                self._lose_session()
                                if self._dead >= RECOVER_AFTER:
                                    self._dead = 0
                                    self.hdev = None
                                    self._recover()
                elif getattr(self, '_last_raw', None):
                    with self.lock:
                        self.do.DirectOutput_SetImage(self.hdev, FW_BASE, 0,
                                                     320 * 240 * 3, self._last_raw)
                    self._last_ok = time.time()
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
        self._begin_overlay()
        self.mode = 'video'
        self.video_stop.clear()
        n = 0
        next_t = time.monotonic()
        try:
            while self.mode == 'video' and not self.video_stop.is_set():
                ret, frame = cap.read()
                if not ret:
                    break
                self._send(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                n += 1
                next_t += 1.0 / fps          # cadence sur l'horloge : vitesse reelle
                delay = next_t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.monotonic()
        finally:
            cap.release()
            self._end_overlay()
        return {'frames': n, 'stopped': self.video_stop.is_set()}


D = None       # le daemon : cree par main() (aucun effet de bord a l'import)


def _shutdown_server():
    time.sleep(0.4)                         # laisse la reponse HTTP partir
    srv = getattr(H, 'server', None)
    if srv is not None:
        srv.shutdown()                      # main() retourne -> atexit -> Deinitialize


class H(BaseHTTPRequestHandler):
    server = None
    server_version = 'fipd/3'

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _allowed(self):
        """Requete strictement locale : un navigateur web (Origin present, y
        compris 'null') ou un Host qui n'est pas loopback (DNS rebinding) est
        refuse — sinon une page web pourrait reecrire /bindings, donc injecter
        des touches, piloter /app/* ou lire le contexte de l'ecran."""
        if self.headers.get('Origin'):
            return False
        host = (self.headers.get('Host') or '').strip().lower()
        if not host:
            return True                     # client HTTP/1.0 sans Host
        for h in LOCAL_HOSTS:
            if host == h or (host.startswith(h + ':') and host[len(h) + 1:].isdigit()):
                return True
        return False

    def do_GET(self):
        if not self._allowed():
            return self._json({'error': 'origine refusee'}, 403)
        path = urlsplit(self.path).path
        if path == '/buttons':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            q = []
            D.sse.append(q)
            beat = time.time()
            try:
                while True:
                    while q:
                        self.wfile.write(('data: ' + json.dumps(q.pop(0)) + '\n\n').encode())
                    self.wfile.flush()
                    time.sleep(0.1)
                    if time.time() - beat > 15:     # battement : detecte un client parti
                        self.wfile.write(b': ping\n\n')
                        self.wfile.flush()
                        beat = time.time()
            except Exception:
                pass
            finally:
                if q in D.sse:
                    D.sse.remove(q)
        elif path == '/events':
            self._json(D.events[-50:])
        elif path == '/bindings':
            D.bindings.reload()
            self._json(D.bindings.cfg)
        elif path == '/keys':
            self._json({'keys': keysend.key_names(),
                        'exemple': 'ctrl+shift+s, volumemute, f5, alt+tab'})
        elif path == '/apps':
            D.bindings.reload()
            self._json({'apps': [{'index': i, 'name': a.name, 'title': a.title,
                                  'external': getattr(a, '_external', False)}
                                 for i, a in enumerate(D.apps)],
                        'page': D.page, 'mode': D.mode,
                        'ok_age': D.ok_age(),          # s depuis la derniere image acceptee
                        'recoveries': D.recoveries})
        elif path.startswith('/app/'):
            import inspect
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
        elif path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            ok = D.ok_age()
            body = ('<html><body style="font-family:sans-serif;background:#111;color:#eee">'
                    '<h2>FIPd v3</h2><p>Status : ' + html.escape(D.status) +
                    f' (recoveries: {D.recoveries})</p><p>Mode : ' + html.escape(D.mode) +
                    '</p><p>Page : ' + str(D.page + 1) + '/' + str(D._npages()) + '</p>'
                    '<p>Dernière image acceptée par le FIP : ' +
                    ('jamais' if ok is None else f'il y a {ok} s') + '</p>'
                    '<p>POST /screen (image) - POST /video - POST /ui - POST /led - '
                    'POST /page - POST /apps/reload - POST /exit - '
                    'GET /buttons (SSE) - GET /apps</p></body></html>').encode('utf-8')
            self.wfile.write(body)
        else:
            self._json({'error': 'inconnu'}, 404)

    def do_POST(self):
        if not self._allowed():
            return self._json({'error': 'origine refusee'}, 403)
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_UPLOAD:
            return self._json({'error': 'corps trop volumineux'}, 413)
        body = self.rfile.read(n) if n else b''
        path = urlsplit(self.path).path
        try:
            from PIL import Image
            if path == '/screen':
                if urlsplit(self.path).query:
                    url = (parse_qs(urlsplit(self.path).query).get('url') or [''])[0]
                    if not re.match(r'https?://', url, re.I):
                        return self._json({'error': 'url= doit etre une adresse http(s)'}, 400)
                    body = _download(url)
                D.show_image(Image.open(io.BytesIO(body)))
                return self._json({'ok': True, 'mode': D.mode, 'page': D._overlay_from})
            if path == '/video':
                import tempfile
                suffix = os.path.splitext(self.headers.get('X-Filename', ''))[1] or '.mp4'
                if not re.fullmatch(r'\.\w{1,5}', suffix):
                    suffix = '.mp4'
                tmp = os.path.join(tempfile.gettempdir(), 'fipd_vid' + suffix)
                with open(tmp, 'wb') as f:
                    f.write(body)
                r = D.play_video(tmp)
                return self._json({'ok': True, 'frames': r['frames'], 'stopped': r['stopped']})
            if path == '/ui':
                D.video_stop.set()
                D._end_overlay()
                return self._json({'ok': True, 'mode': D.mode, 'page': D.page})
            if path == '/led':
                dd = json.loads(body or b'{}')
                if not isinstance(dd, dict):
                    raise ValueError('objet JSON attendu')
                if dd.get('scan'):
                    return self._json({'scan': D.scan_leds()})
                idx = int(dd.get('index', 0))
                on = bool(dd.get('on', True))
                hr = D.set_led(idx, on)
                return self._json({'ok': hr == 0, 'index': idx, 'on': on,
                                   'hr': f'{hr:08X}', 'page': D.page})
            if path == '/page':
                dd = json.loads(body or b'{}')
                D.video_stop.set()
                D._end_overlay()
                D._switch_page(max(0, min(D._npages() - 1, int(dd.get('page', 0)))))
                return self._json({'ok': True, 'page': D.page})
            if path == '/apps/reload':
                dd = json.loads(body.decode('utf-8')) if body.strip() else {}
                only = None
                if isinstance(dd, dict) and dd.get('all'):
                    only = '*'
                elif isinstance(dd, dict) and dd.get('app'):
                    only = str(dd['app'])
                return self._json(D.reload_apps(only))
            if path == '/device/reset':
                threading.Thread(target=D._reattach, daemon=True).start()
                return self._json({'ok': True, 'started': True})
            if path == '/exit':
                self._json({'ok': True})
                threading.Thread(target=_shutdown_server, daemon=True).start()
                return
            if path.startswith('/app/'):
                name = path[len('/app/'):].strip('/')
                app = next((a for a in D.apps if a.name == name), None)
                if app is None:
                    return self._json({'error': f'app inconnue: {name}'}, 404)
                h = getattr(app, 'http_post', None)
                if h is None:
                    return self._json({'error': f'{name} n expose pas http_post'}, 405)
                try:
                    payload = json.loads(body or b'{}')
                    return self._json(h(payload))
                except Exception as e:
                    return self._json({'error': str(e)}, 500)
            if path == '/bindings':
                try:
                    cfg = json.loads(body)
                except Exception as e:
                    return self._json({'error': f'json invalide: {e}'}, 400)
                try:
                    if not isinstance(cfg, dict):
                        raise ValueError('objet JSON attendu')
                    pages = cfg.get('pages', {})
                    if not isinstance(pages, dict):
                        raise ValueError('pages doit etre un objet')
                    scopes = [cfg.get('bindings', {})] + list(pages.values())
                    for scope in scopes:
                        if not isinstance(scope, dict):
                            raise ValueError('bindings et pages.* doivent etre des objets')
                    for scope in scopes:
                        for name, combo in scope.items():
                            if not combo:
                                continue
                            if name not in SOFTBUTTONS.values():
                                raise ValueError(f'bouton inconnu: {name}')
                            keysend.parse_combo(combo)     # KeyError si touche inconnue
                except (KeyError, TypeError, AttributeError, ValueError) as e:
                    return self._json({'error': f'bindings invalides: {e}'}, 400)
                D.bindings.cfg = cfg
                D.bindings.save()
                return self._json({'ok': True, 'bindings': D.bindings.cfg})
            return self._json({'error': 'inconnu'}, 404)
        except Exception as e:
            return self._json({'error': str(e)}, 500)


class Server(ThreadingHTTPServer):
    # Pas de SO_REUSEADDR : sous Windows il laisserait un second fipd s'installer
    # sur le meme port (au lieu d'echouer clairement).
    allow_reuse_address = False
    daemon_threads = True


def main():
    import atexit
    print('fipd v3 — connexion a la pile officielle (service DirectOutputService)...', flush=True)
    st = service_state(SERVICE)
    t0 = time.time()
    while st == 'START_PENDING' and time.time() - t0 < 30:   # il demarre : on attend
        time.sleep(1)
        st = service_state(SERVICE)
    if st != 'RUNNING':
        print(f'ECHEC : le service {SERVICE} est {st or "introuvable"} — sans lui, '
              'DirectOutput_Initialize resterait bloque.', flush=True)
        print(f'Demarrez-le : sc start {SERVICE}  (admin)', flush=True)
        sys.exit(1)
    try:
        srv = Server(('127.0.0.1', PORT), H)     # port exclusif AVANT de toucher au FIP
    except OSError as e:
        print(f'ECHEC : port {PORT} indisponible ({e}) — un autre fipd tourne deja ?', flush=True)
        sys.exit(1)
    H.server = srv
    global D
    D = Daemon()
    ok = D.start()
    atexit.register(D.shutdown)             # libere nos pages
    if ok:
        print(f'Pret : http://127.0.0.1:{PORT} — {D.status}', flush=True)
    else:
        print(f'Pret : http://127.0.0.1:{PORT} — {D.status} '
              f'(nouvelles tentatives toutes les {RECOVER_EVERY_S:.0f} s)', flush=True)
    print(f'{D._npages()} applications : ' + ', '.join(a.title for a in D.apps), flush=True)
    print('Molette gauche = page. S1 = stop video. GET /apps pour la liste.', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
