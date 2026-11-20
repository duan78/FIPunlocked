"""
fipui.py — Mini-framework d'applications 100% FIP.

Une App = une page du panneau. Elle combine :
  - rendu ecran        render(d)          (PIL ImageDraw, fond deja peint)
  - evenements         on_event(name, pressed)   (S1..S6, select, knobs — arêtes)
  - LEDs persistantes  set_led(index, on)  (etat souhaite, re-applique a l'entree
                        sur la page : le RemovePage+AddPage efface les tampons)
  - alertes visuelles  alert(msg)          (bandeau + clignotement LED 8)

Le daemon (fipd3) fournit : la page courante (regle E_PAGENOTACTIVE), le thread
d'affichage unique (jamais de DirectOutput depuis un callback), la pagination
molette gauche, le keepalive, les bindings clavier opt-in.
"""
import os
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


class App:
    """Sous-classez et remplissez : name, title, render(), on_event(), tick()."""
    name = 'app'
    title = 'App'
    show_footer = True         # le daemon ajoute "page N/M" si True
    led_feedback = None        # None = suit la config globale, sinon bool

    def __init__(self, daemon):
        self.d = daemon
        self.leds = {}         # etat LED souhaite {index: bool}
        self._alert = None     # (message, t0)

    # ---------- cycle de vie ----------
    def on_enter(self):
        """Appellee quand la page devient courante (molette ou boutons centraux)."""
        pass

    def on_leave(self):
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
        self._alert = (str(msg), time.time())

    def alert_active(self):
        return bool(self._alert) and (time.time() - self._alert[1] < ALERT_SECONDS)

    def draw_alert(self, d, font):
        """Bandeau clignotant en haut d'ecran (appelle par le daemon)."""
        if not self._alert:
            return
        msg, t0 = self._alert
        age = time.time() - t0
        if age >= ALERT_SECONDS:
            self._alert = None
            self.restore_leds()             # restaure les LEDs (fin clignotement)
            return
        if int(age * 4) % 2 == 0:           # ~2 Hz
            d.rectangle((0, 0, 319, 26), fill=RED)
            d.text((160, 13), '!  ' + msg[:30], fill=(255, 255, 255),
                   font=font, anchor='mm')


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
        f = ImageFont.load_default()
        for p in (r'C:\Windows\Fonts\consola.ttf', r'C:\Windows\Fonts\arial.ttf'):
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, size)
                    break
                except Exception:
                    pass
        _font_cache[size] = f
    return _font_cache[size]


def draw_bar(d, x0, y0, x1, y1, ratio, color=BLUE):
    """Jauge horizontale (ratio 0..1)."""
    ratio = max(0.0, min(1.0, ratio))
    d.rectangle((x0, y0, x1, y1), outline=(70, 75, 92))
    d.rectangle((x0 + 1, y0 + 1, x0 + 1 + int((x1 - x0 - 2) * ratio), y1 - 1),
                fill=color)
