"""
fipui.py — Mini-framework d'applications 100 % FIP.

Une App = une page du panneau. Elle combine :
  - rendu ecran        render(d)          (PIL ImageDraw, fond deja peint)
  - evenements         on_event(name, pressed)   (S1..S6, select, knobs — arêtes)
  - LEDs persistantes  set_led(index, on)  (etat souhaite, re-applique a l'entree
                        sur la page : le RemovePage+AddPage efface les tampons)
  - alertes visuelles  alert(msg)          (bandeau + clignotement LED 8)
  - travail de fond    poll(fn, interval)  (thread qui ne tourne QUE pendant que
                        la page est affichée — coût nul page masquée)

Le daemon (fipd3) fournit : la page courante (regle E_PAGENOTACTIVE), le thread
d'affichage unique (jamais de DirectOutput depuis un callback), la pagination
molette gauche, le keepalive, les bindings clavier opt-in.
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

# ---- palette partagee ----
BG = (18, 22, 38)
FG = (235, 238, 245)
DIM = (120, 125, 140)
ACCENT = (255, 210, 80)
GREEN = (70, 200, 120)
RED = (220, 90, 80)
BLUE = (90, 160, 230)

ALERT_SECONDS = 3.0
ALERT_LED = 8                 # LED utilisee pour le clignotement d'alerte
SCREEN_W = 320


class App:
    """Sous-classez et remplissez : name, title, render(), on_event(), tick()."""
    name = 'app'
    title = 'App'
    show_footer = True         # le daemon ajoute "page N/M" si True
    led_feedback = None        # None = suit la config globale, sinon bool

    def __init__(self, daemon):
        self.d = daemon
        self.leds = {}         # etat LED souhaite {index: bool}
        self._alert = None     # [message, t0] ; t0 = None tant que non affichee
        self._stop = threading.Event()     # setté par le daemon au reload
        self._shown = threading.Event()    # la page est-elle affichée ?
        self._polls = []                   # events de réveil des threads poll()
        self._poll_err_t = 0.0             # anti-spam des erreurs de poll

    # ---------- cycle de vie ----------
    def on_enter(self):
        """Appellee quand la page devient courante (molette ou boutons centraux)."""
        self._shown.set()

    def on_leave(self):
        self._shown.clear()

    def shown(self):
        """True ssi la page est celle du FIP en ce moment."""
        return self._shown.is_set()

    def hold_tabs(self):
        """Page affichée et elle lit les onglets des navigateurs (uiatabs) :
        leur lecture repasse à sa cadence normale, avec une passe immédiate.
        Idempotent (on_enter peut être rappelé par une auto-guérison)."""
        if getattr(self, '_tabs_held', False):
            return
        self._tabs_held = True
        try:
            import uiatabs
            uiatabs.acquire()
        except Exception:
            pass

    def release_tabs(self):
        """Page quittée : la lecture des onglets ralentit (elle reste tiède)."""
        if not getattr(self, '_tabs_held', False):
            return
        self._tabs_held = False
        try:
            import uiatabs
            uiatabs.release()
        except Exception:
            pass

    def on_event(self, name, pressed):
        """Arrete bouton/molette : name in S1..S6, select, *_cw, *_ccw."""
        pass

    def tick(self):
        """Appelle a chaque boucle d'affichage (~2 s, ~0.3 s si alerte active)."""
        if self.alert_active():
            self.d.set_led(ALERT_LED, int(time.time() * 4) % 2 == 0)

    def render(self, d):
        """Dessine la page (320x240, fond BG deja peint)."""
        pass

    def poll(self, fn, interval):
        """Lance un thread qui exécute fn() toutes les `interval` secondes,
        UNIQUEMENT pendant que la page est affichée (une passe immédiate à
        l'entrée). Une exception de fn est loggée (10 s max entre deux messages)
        et le thread survit. Le thread s'arrête au reload (self._stop)."""
        wake = threading.Event()
        self._polls.append(wake)

        def run():
            while not self._stop.is_set():
                while not self._shown.is_set() and not self._stop.is_set():
                    self._shown.wait(0.5)       # page masquée : endormi, coût nul
                if self._stop.is_set():
                    break
                try:
                    fn()
                except Exception as e:
                    if time.time() - self._poll_err_t > 10:
                        self._poll_err_t = time.time()
                        print('[%s] poll: %r' % (self.name, e), flush=True)
                wake.clear()
                wake.wait(interval)             # on_enter / on_leave le réveillent

        t = threading.Thread(target=run, name='%s-poll' % self.name, daemon=True)
        t.start()
        return t

    def _wake_polls(self):
        for w in self._polls:
            w.set()

    # ---------- LEDs ----------
    def set_led(self, index, on):
        """Etat voulu (re-applique a l'entree de page par restore_leds) ;
        applique physiquement SEULEMENT si notre page est la page courante —
        sinon un thread de fond allumerait sa LED sur la page affichee."""
        self.leds[index] = bool(on)
        try:
            if self.d.apps[self.d.page] is self:
                self.d.set_led(index, on)
        except Exception:
            pass

    def apply_leds(self):
        for idx, on in self.leds.items():
            self.d.set_led(idx, on)

    def restore_leds(self):
        """Applique l'etat souhaite ET eteint les LEDs non revendiquees (1-8) :
        les LEDs fixes 7-8 retombent ainsi a OFF sur les pages qui ne les
        utilisent pas (etat stock = allume)."""
        self.apply_leds()
        for led in range(1, 9):
            if led not in self.leds:
                self.d.set_led(led, False)

    def clear_leds(self):
        for idx in list(self.leds):
            self.set_led(idx, False)

    # ---------- alertes ----------
    def alert(self, msg):
        """Bandeau 3 s + clignotement LED 8. Lancée depuis une page qui n'est pas
        courante, l'alerte ATTEND d'être affichée (elle n'est pas perdue)."""
        self._alert = [str(msg), None]
        try:
            d = self.d
            if getattr(d, 'mode', 'ui') == 'ui' and d.apps[d.page] is self:
                d._redraw_now()
        except Exception:
            pass

    def alert_active(self):
        return self._alert is not None

    def draw_alert(self, d, big):
        """Bandeau clignotant en haut d'ecran (appelle par le daemon) : la
        police est réduite, puis le message tronqué, pour tenir dans 320 px.
        `big` = police d'origine (ne pas l'appeler `font` : cela masquerait
        la fonction font() du module)."""
        if not self._alert:
            return
        if self._alert[1] is None:              # premier affichage : chrono
            self._alert[1] = time.time()
        msg, t0 = self._alert
        age = time.time() - t0
        if age >= ALERT_SECONDS:
            self._alert = None
            self.restore_leds()                 # restaure les LEDs (fin clignotement)
            return
        if int(age * 4) % 2 == 0:               # ~2 Hz
            d.rectangle((0, 0, 319, 26), fill=RED)
            text = '!  ' + msg
            for f in (big, font(17), font(14)):
                if d.textlength(text, font=f) <= SCREEN_W - 12:
                    break
            else:
                f = font(14)
                while len(text) > 3 and d.textlength(text + '…', font=f) > SCREEN_W - 12:
                    text = text[:-1]
                text += '…'
            d.text((160, 13), text, fill=(255, 255, 255), font=f, anchor='mm')


# ---- widgets (fonctions de dessin) ----

def draw_header(d, font, title, right=''):
    """Barre de titre standard."""
    d.rectangle((0, 0, 319, 21), fill=(28, 33, 50))
    d.text((8, 11), title, font=font, fill=ACCENT, anchor='lm')
    if right:
        d.text((312, 11), right, font=font, fill=DIM, anchor='rm')


def draw_menu(d, font, items, cursor, states=None, y0=36, row_h=30):
    """Liste type menu : molette = curseur, points = etats on/off."""
    n = len(items)
    visible = 6
    lo = 0
    if n > visible:
        lo = max(0, min(cursor - visible // 2, n - visible))
    for i in range(lo, min(n, lo + visible)):
        y = y0 + (i - lo) * row_h
        sel = (i == cursor)
        if sel:
            d.rectangle((8, y, 307, y + row_h - 4), outline=BLUE, width=2)
            d.polygon([(310, y + 5), (318, y + row_h // 2 - 2),
                       (310, y + row_h - 9)], fill=BLUE)
        d.text((16, y + row_h // 2 - 2), items[i], font=font,
               fill=FG if sel else DIM, anchor='lm')
        if states is not None and i in states:
            on = states[i]
            d.ellipse((284, y + 7, 300, y + 23),
                      fill=GREEN if on else (45, 50, 64),
                      outline=(160, 220, 180) if on else (80, 85, 100))


# ---- polices (cache partage, pour les apps tierces) ----
_font_cache = {}


def font(size):
    """Police a taille fixe (consolas -> arial -> defaut), mise en cache."""
    if size not in _font_cache:
        from PIL import ImageFont
        f = None
        for p in (r'C:\Windows\Fonts\consola.ttf', r'C:\Windows\Fonts\arial.ttf'):
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, size)
                    break
                except Exception:
                    pass
        if f is None:
            try:
                f = ImageFont.load_default(size=size)
            except TypeError:          # Pillow < 10.1 : police par defaut fixe
                f = ImageFont.load_default()
        _font_cache[size] = f
    return _font_cache[size]


def draw_bar(d, x0, y0, x1, y1, ratio, color=BLUE):
    """Jauge horizontale (ratio 0..1) ; à 0 % seul le cadre est tracé."""
    ratio = max(0.0, min(1.0, ratio))
    d.rectangle((x0, y0, x1, y1), outline=(70, 75, 92))
    w = int((x1 - x0 - 2) * ratio)
    if w > 0:
        d.rectangle((x0 + 1, y0 + 1, x0 + 1 + w, y1 - 1), fill=color)


# ---- presse-papiers (ctypes, aucune dependance) ----
_u32 = ctypes.WinDLL('user32')
_k32 = ctypes.WinDLL('kernel32')
_u32.OpenClipboard.argtypes = [wt.HWND]
_u32.OpenClipboard.restype = wt.BOOL
_u32.CloseClipboard.restype = wt.BOOL
_u32.EmptyClipboard.restype = wt.BOOL
_u32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
_u32.SetClipboardData.restype = wt.HANDLE
_k32.GlobalAlloc.restype = ctypes.c_void_p
_k32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalLock.argtypes = [ctypes.c_void_p]
_k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_k32.GlobalFree.restype = ctypes.c_void_p
_k32.GlobalFree.argtypes = [ctypes.c_void_p]
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 2


def clipboard_set(text, tries=12, wait=0.02):
    """Pose `text` dans le presse-papiers. Retourne True si posé.
    Réessaie ~0,25 s : le presse-papiers est souvent verrouillé un instant par
    un autre logiciel (gestionnaires de presse-papiers, bureaux distants...)."""
    for _ in range(tries):
        if _u32.OpenClipboard(None):
            break
        time.sleep(wait)
    else:
        return False
    try:
        _u32.EmptyClipboard()
        data = str(text).encode('utf-16-le') + b'\x00\x00'
        h = _k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = _k32.GlobalLock(h)
        if not p:
            _k32.GlobalFree(h)
            return False
        ctypes.memmove(p, data, len(data))
        _k32.GlobalUnlock(h)
        ok = _u32.SetClipboardData(CF_UNICODETEXT, h)
        if not ok:                      # le systeme devient proprietaire si ok
            _k32.GlobalFree(h)
        return bool(ok)
    finally:
        _u32.CloseClipboard()
