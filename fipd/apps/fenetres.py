"""
fenetres.py — toutes les FENETRES ouvertes + tous les ONGLETS des navigateurs,
avec actions contextuelles (charte 30/70 : actions a gauche, liste a droite).

Sources :
  - fenetres : EnumWindows — visible + titre, z-order, processus, etats
    (premier plan / minimise / toujours devant)
  - onglets Chrome/Edge : arbre d'accessibilite Windows (uiatabs.py, sans
    extension) — titres, onglet actif, URL de l'onglet actif

Colonnes :
  GAUCHE (30 %) = keycaps contextuels de l'entree SELECTIONNEE (curseur) :
    fenetre : Focus / Minimiser / Devant / Fermer (2x) / Tuer (2x) / Copier titre
    onglet  : Activer / Copier titre / Copier URL (onglet actif) / Google / Vol- / Vol+
  DROITE (70 %) = liste : FENETRES (n) puis ONGLETS (n), curseur = molette
  droite (ligne surlignee, vert = onglet actif).

Les confirmations en deux appuis sont attachees a LEUR cible : si le curseur
bouge entre les deux, il faut re-armer.

L'enumeration ne tourne QUE pendant que la page est affichee (fipui poll).
"""
import ctypes
import time
from ctypes import wintypes as wt

import psutil

import charte
import fipui
from fipui import App

u32 = ctypes.WinDLL('user32')
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.IsIconic.argtypes = [wt.HWND]
u32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = wt.LONG
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wt.UINT]
u32.SetWindowPos.restype = wt.BOOL
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.ShowWindow.restype = wt.BOOL
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.SetForegroundWindow.restype = wt.BOOL
u32.GetForegroundWindow.restype = wt.HWND
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.PostMessageW.restype = wt.BOOL

ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
u32.EnumWindows.argtypes = [ENUMPROC, wt.LPARAM]
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x0080
WS_EX_TOPMOST = 0x0008
SWP_NOMOVE_NOSIZE = 0x0003
SW_RESTORE = 9
SW_MINIMIZE = 6
WM_CLOSE = 0x0010

POLL_S = 1.5
TABS_STALE_S = 12.0
PROTECTED = {'explorer.exe', 'python.exe', 'pythonw.exe', 'dwm.exe',
             'svchost.exe', 'csrss.exe', 'lsass.exe', 'services.exe',
             'wininit.exe', 'winlogon.exe', 'smss.exe'}


def _copy(text, msg):
    """Presse-papiers : la banniere dit la verite (le presse-papiers peut etre occupe)."""
    return msg if fipui.clipboard_set(text) else 'presse-papiers occupe'


def _enum_windows():
    """Fenetres visibles avec titre (hors toolwindows), dans l'ordre du z-order."""
    fg = u32.GetForegroundWindow()
    wins = []

    @ENUMPROC
    def cb(h, lp):
        if not u32.IsWindowVisible(h):
            return True
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(h, buf, 512)
        title = buf.value.strip()
        if not title:
            return True
        ex = u32.GetWindowLongW(h, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW:
            return True
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value).name().lower()
        except Exception:
            return True
        wins.append({'kind': 'win', 'hwnd': h, 'pid': pid.value,
                     'proc': proc, 'title': title,
                     'min': bool(u32.IsIconic(h)),
                     'top': bool(ex & WS_EX_TOPMOST),
                     'fg': h == fg})
        return True

    try:
        u32.EnumWindows(cb, 0)
    except Exception:
        pass
    return wins


class FenetresApp(App):
    name = 'fenetres'
    title = 'FENETRES'
    show_footer = False

    def __init__(self, daemon):
        super().__init__(daemon)
        self.wins = []               # entrees fenetres (dicts)
        self.tabs = []               # onglets navigateurs (uiatabs)
        self.tabs_t = 0.0
        self.cursor = 0              # entree selectionnee (0..n-1)
        self._armed = None           # (action, cible, t0) : cible = hwnd ou pid
        self.poll(self._enumerate, POLL_S)   # page affichée seulement

    def on_enter(self):
        self.hold_tabs()                    # onglets : cadence normale + passe immédiate

    def on_leave(self):
        self.release_tabs()                 # page masquée : le poll s'endort déjà

    # ---------- enumeration (une passe) ----------

    def _enumerate(self):
        self.wins = _enum_windows()
        tabs = []
        try:
            import uiatabs
            for e in uiatabs.snapshot():
                for t in e['tabs']:
                    tabs.append({'kind': 'tab', 'title': t['title'],
                                 'active': t['active'],
                                 'url': e['url'] if t['active'] else '',
                                 'hwnd': e['hwnd']})
        except Exception:
            pass
        self.tabs = tabs
        self.tabs_t = time.time() if tabs else 0.0

    # ---------- entrees unifiees ----------

    def _entries(self):
        """[fenetres...] + [onglets...] (onglets si le flux UIA est frais)."""
        ent = list(self.wins)
        if time.time() - self.tabs_t < TABS_STALE_S:
            ent += self.tabs
        return ent

    def _sel(self):
        ent = self._entries()
        if not ent:
            return None
        self.cursor = max(0, min(self.cursor, len(ent) - 1))
        return ent[self.cursor]

    # ---------- commandes ----------

    def http_post(self, payload):
        return {'ok': False, 'error': 'onglets via UIA : aucune commande distante'}

    def http_get(self, q=None):
        return {'windows': len(self.wins), 'tabs': len(self.tabs),
                'uia': True}

    # ---------- boutons ----------

    def _confirm(self, action, target):
        """Double appui : le second doit viser la MEME cible (hwnd / pid) — si le
        curseur a bouge entre-temps, on re-arme au lieu de fermer ou tuer."""
        now = time.time()
        if (self._armed and self._armed[0] == action and self._armed[1] == target
                and now - self._armed[2] < 3.0):
            self._armed = None
            return True
        self._armed = (action, target, now)
        self.alert('re-appuyez pour ' + action)
        return False

    def on_event(self, name, pressed):
        if not pressed:
            return
        if name == 'right_cw':
            self.cursor += 1
            return
        if name == 'right_ccw':
            self.cursor = max(0, self.cursor - 1)
            return
        if name not in ('S1', 'S2', 'S3', 'S4', 'S5', 'S6'):
            return
        e = self._sel()
        if e is None:
            return
        n = int(name[1])
        if e['kind'] == 'win':
            h = e['hwnd']
            if n == 1:                                   # focus
                if e['min']:
                    u32.ShowWindow(h, SW_RESTORE)
                if not u32.SetForegroundWindow(h):
                    u32.ShowWindow(h, SW_MINIMIZE)       # dernier recours
                    u32.ShowWindow(h, SW_RESTORE)
                    u32.SetForegroundWindow(h)
                self.alert('focus')
            elif n == 2:
                u32.ShowWindow(h, SW_MINIMIZE)
            elif n == 3:                                 # topmost
                on = not e['top']
                if u32.SetWindowPos(h, wt.HWND(-1 if on else -2),
                                     0, 0, 0, 0, SWP_NOMOVE_NOSIZE):
                    e['top'] = on
                    self.alert('devant: ' + ('OUI' if on else 'NON'))
            elif n == 4:                                 # fermer (2x)
                if self._confirm('close', h):
                    u32.PostMessageW(h, WM_CLOSE, 0, 0)
                    self.alert('fermeture')
            elif n == 5:                                 # tuer (2x)
                if self._confirm('kill', e['pid']):
                    if e['proc'] in PROTECTED:
                        self.alert('protege')
                    else:
                        try:
                            psutil.Process(e['pid']).kill()
                            self.alert(e['proc'] + ' arrete')
                        except Exception as ex:
                            self.alert(str(ex)[:24])
            elif n == 6:
                self.alert(_copy(e['title'], 'titre copie'))
        else:                                             # onglet navigateur
            import webbrowser
            if n == 1:                                    # activer (UIA)
                import uiatabs
                if uiatabs.select(e['hwnd'], e['title']):
                    self.alert('onglet active')
                else:
                    self.alert('UIA: echec selection')
            elif n == 2:
                self.alert(_copy(e['title'], 'titre copie'))
            elif n == 3:
                if e.get('url'):
                    self.alert(_copy(e['url'], 'URL copiee'))
                else:
                    self.alert('URL: onglet actif seulement')
            elif n == 4:
                webbrowser.open('https://www.google.com/search?q=' +
                                e['title'].replace(' ', '+'))
            elif n in (5, 6):                             # volume systeme
                import keysend
                keysend.tap('volumedown' if n == 5 else 'volumeup')

    # ---------- rendu ----------

    def render(self, d):
        F_T = fipui.font(11)
        sel = self._sel()
        # ---- GAUCHE : keycaps contextuels
        if sel and sel['kind'] == 'win':
            armed = self._armed[0] if (
                self._armed and time.time() - self._armed[2] < 3.0) else None
            caps = [
                (charte.ico_play, 'Focus', '', None, False),
                (charte.ico_min, 'Minimiser', '', None, False),
                (charte.ico_pin, 'Devant', 'ON' if sel['top'] else '',
                 charte.BLUE if sel['top'] else None, sel['top']),
                (charte.ico_x, 'Fermer', '2x',
                 charte.ORANGE if armed == 'close' else None,
                 armed == 'close'),
                (charte.ico_x, 'Tuer', '2x', charte.RED if armed == 'kill'
                 else None, armed == 'kill'),
                (charte.ico_copy, 'Titre', '', None, False),
            ]
        elif sel:
            caps = [
                (charte.ico_play, 'Activer', '', None, sel.get('active',
                                                               False)),
                (charte.ico_copy, 'Titre', '', None, False),
                (charte.ico_link, 'URL', '' if sel.get('url') else 'n/a',
                 None, False),
                (charte.ico_home, 'Google', '', None, False),
                (charte.ico_min, 'Vol-', '', None, False),
                (charte.ico_min, 'Vol+', '', None, False),
            ]
        else:
            caps = [(None, '-', '', None, False)] * 6
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        # ---- DROITE : liste lignes = [entete fenetres] fenetres...
        #               [entete onglets] onglets...
        charte.panel(d, (98, 4, 317, 238))
        nw = len(self.wins)
        nt = len(self.tabs) if time.time() - self.tabs_t < TABS_STALE_S else 0
        d.rounded_rectangle((289, 10, 313, 25), radius=7, fill=charte.CARD,
                            outline=charte.TEAL)
        d.text((301, 18), '{}+{}'.format(nw, nt), font=F_T, fill=charte.TEAL,
               anchor='mm')
        lines = [('hdr', 'FENETRES ({})'.format(nw))]
        lines += [('win', e) for e in self.wins]
        if nt:
            lines.append(('hdr', 'ONGLETS ({})'.format(nt)))
            lines += [('tab', e) for e in self.tabs]
        sel_disp = next((i for i, ln in enumerate(lines)
                         if ln[0] != 'hdr' and ln[1] is sel), 0)
        max_lines = 11
        start = 0
        if len(lines) > max_lines:
            start = max(0, min(sel_disp - max_lines // 2,
                               len(lines) - max_lines))
        y = 26
        for kind, e in lines[start:start + max_lines]:
            if kind == 'hdr':
                d.text((106, y + 2), e, font=F_T, fill=charte.DIM,
                       anchor='lm')
                y += 16
                continue
            if e is sel:
                d.rounded_rectangle((102, y - 8, 313, y + 8), radius=5,
                                    fill=charte.CARD_HI,
                                    outline=charte.BLUE, width=1)
            if kind == 'win':
                d.ellipse((106, y - 4, 113, y + 3),
                          fill=charte._hash_color(e['proc']))
                t = e['title']
                while t and d.textlength(t, font=F_T) > 150:
                    t = t[:-1]
                d.text((118, y), t, font=F_T,
                       fill=charte.FG if e['fg'] else charte.DIM,
                       anchor='lm')
                if e['top']:
                    charte.ico_pin(d, 278, y, 3, charte.BLUE)
                if e['min']:
                    d.text((288, y), 'min', font=F_T, fill=(90, 95, 110),
                           anchor='lm')
                d.text((313, y), e['proc'][:9], font=F_T,
                       fill=(100, 108, 120), anchor='rm')
            else:
                col = charte.GREEN if e['active'] else charte.BLUE
                d.ellipse((106, y - 4, 113, y + 3), fill=col)
                host = e['url'].split('/')[2][:16] \
                    if e.get('url', '').startswith('http') else ''
                # le domaine (a droite) reserve sa place : jamais sur le titre
                maxw = 170 - (int(d.textlength(host, font=F_T)) + 8 if host else 0)
                t = e['title']
                while t and d.textlength(t, font=F_T) > maxw:
                    t = t[:-1]
                d.text((118, y), t, font=F_T,
                       fill=charte.FG if e['active'] else charte.DIM,
                       anchor='lm')
                if host:
                    d.text((313, y), host, font=F_T, fill=(100, 140, 200),
                           anchor='rm')
            y += 17
        if len(lines) > max_lines:
            d.rectangle((314, 26, 316, 236), fill=(30, 35, 50))
            th = 210 * max_lines // len(lines)
            ty = 26 + (210 - th) * start // max(1, len(lines) - max_lines)
            d.rectangle((314, ty, 316, ty + th), fill=charte.BLUE)
