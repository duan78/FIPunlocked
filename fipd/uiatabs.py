"""
uiatabs.py — onglets et URL des fenetres Chrome/Edge SANS extension :
lecture de l'arbre d'accessibilite Windows (UI Automation).
- thread de rafraichissement (0,6 s) : par fenetre navigateur : titre, URL
  (barre d'adresse), onglets, onglet actif
- select(hwnd, titre) : change d'onglet via SelectionItemPattern
Performances : les elements UIA (barre d'adresse, bandeau d'onglets) sont
MIS EN CACHE par fenetre — ils persistent entre les changements d'onglet ;
les rechercher a chaque scan coute 0,5-1 s ET echoue juste apres une
bascule (le titre de fenetre change AVANT l'arbre UIA).
Limitations UIA/Chrome : onglets virtualises (hors bande) absents ; l'URL
n'est connue que pour l'onglet ACTIF de chaque fenetre.
"""
import ctypes
import threading
import time
from ctypes import wintypes as wt

u32 = ctypes.WinDLL('user32')
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM),
                            wt.LPARAM]

BROWSERS = ('chrome.exe', 'msedge.exe')
REFRESH_S = 0.6            # cycle de rafraichissement (scan ~0,1 s cache)
URL_STICKY_S = 8.0         # garder la derniere URL connue si lecture ratee
BROWSER_SUFFIXES = (' - Google Chrome', ' - Microsoft\xad Edge',
                    ' - Mozilla Firefox', ' - Brave', ' - Opera', ' - Vivaldi')

_lock = threading.Lock()
_snap = []                      # [{'hwnd','proc','title','url','tabs':[...] }]
_last_url = {}                  # hwnd -> (url, t) : URL collante anti-flicker
_el_cache = {}                  # hwnd -> {'edit': ctrl, 'strip': ctrl}
_last_state = {}                # hwnd -> {'url','tabs'} : dernier etat UIA
_stop = threading.Event()
_started = False


def _publish(snap):
    with _lock:
        globals()['_snap'] = snap


def _win_title(h):
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(h, buf, 512)
    return buf.value.strip()


def _strip_suffix(title):
    for s in BROWSER_SUFFIXES:
        if title.endswith(s):
            return title[:-len(s)]
    return title


def _find_edit(root, budget=0.4):
    """DFS borne : premier EditControl (barre d'adresse, profondeur ~10).
    NB : les recherches du paquet uiautomation ATTENDENT l'element (timeout
    interne) quand il est absent — une fenetre sans barre d'adresse (PiP,
    DevTools) bloquerait le scan plusieurs secondes."""
    import uiautomation as auto
    t0 = time.time()

    def rec(ctrl, depth):
        if depth > 12 or time.time() - t0 > budget:
            return None
        try:
            kids = ctrl.GetChildren()
        except Exception:
            return None
        for ch in kids:
            if isinstance(ch, auto.EditControl):
                return ch
            r = rec(ch, depth + 1)
            if r is not None:
                return r
        return None

    return rec(root, 0)


def _find_strip(root, base, budget=0.3):
    """DFS borne : le TabControl contenant un onglet nomme comme le titre
    de la fenetre (= bandeau navigateur, pas les onglets de la page web)."""
    import uiautomation as auto
    t0 = time.time()

    def rec(ctrl, depth):
        if depth > 12 or time.time() - t0 > budget:
            return None
        try:
            kids = ctrl.GetChildren()
        except Exception:
            return None
        for ch in kids:
            if isinstance(ch, auto.TabControl):
                names = []
                for t in ch.GetChildren():
                    if isinstance(t, auto.TabItemControl):
                        names.append((t.Name or '').strip())
                if base in names:
                    return ch
            r = rec(ch, depth + 1)
            if r is not None:
                return r
        return None

    return rec(root, 0)


def _cached(hwnd, key, finder):
    """Element UIA par fenetre, cache : le bandeau et la barre d'adresse
    persistent entre les bascules d'onglet."""
    ent = _el_cache.get(hwnd) or {}
    el = ent.get(key)
    if el is not None:
        try:
            _ = el.Name                     # leve si l'element a disparu
            return el
        except Exception:
            el = None
    el = finder()
    if el is not None:
        ent[key] = el
        _el_cache[hwnd] = ent
    return el


def _browser_windows():
    """[(hwnd, proc, titre)] des fenetres navigateurs — 100 % Win32,
    donc IMMUNISEE contre les blocages UIA de Chrome."""
    import psutil
    found = []
    ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    @ENUMPROC
    def cb(h, lp):
        if u32.IsWindowVisible(h) and _win_title(h):
            found.append(h)
        return True

    u32.EnumWindows(cb, 0)
    out = []
    for h in found:
        try:
            pid = wt.DWORD()
            u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            proc = psutil.Process(pid.value).name().lower()
        except Exception:
            continue
        if proc in BROWSERS:
            out.append((h, proc, _win_title(h)))
    return out


def _fast_pass():
    """Publication IMMEDIATE : titres frais (Win32) + URL/onglets du dernier
    etat UIA connu. Le contexte caméléon suit le titre sans attendre que
    Chrome daigne rafraichir son arbre d'accessibilite."""
    snap = []
    for h, proc, title in _browser_windows():
        st = _last_state.get(h) or {}
        snap.append({'hwnd': h, 'proc': proc, 'title': title,
                     'url': st.get('url', ''),
                     'tabs': st.get('tabs', [])})
    _publish(snap)
    return snap


def _uia_pass():
    """Passe UIA (peut bloquer quelques secondes sur une page chargee) :
    met a jour l'URL et les onglets, puis republie."""
    import uiautomation as auto
    base_snap = _fast_pass()
    t0 = time.time()
    out = []
    for ent in base_snap:
        h, title = ent['hwnd'], ent['title']
        base = _strip_suffix(title)
        entry = dict(ent, tabs=[])
        try:
            c = auto.ControlFromHandle(h)
            if c is not None:
                edit = _cached(h, 'edit', lambda: _find_edit(c))
                for _ in range(6):
                    if edit is None:
                        break
                    try:
                        v = edit.GetValuePattern().Value
                        if v and (v.startswith('http')
                                  or ('.' in v and ' ' not in v)):
                            if not v.startswith('http'):
                                v = 'https://' + v
                            entry['url'] = v
                            _last_url[h] = (v, time.time())
                            break
                    except Exception:
                        pass
                    try:
                        edit = edit.GetNextSiblingControl()
                    except Exception:
                        break
                if not entry['url']:
                    last = _last_url.get(h)
                    if last and time.time() - last[1] < URL_STICKY_S:
                        entry['url'] = last[0]
                tabs_ctrl = _cached(
                    h, 'strip', lambda: _find_strip(c, base))
                if tabs_ctrl is not None:
                    for tab in tabs_ctrl.GetChildren():
                        name = (tab.Name or '').strip()
                        if not name:
                            continue
                        entry['tabs'].append({
                            'title': name,
                            'active': name == base
                            or base.startswith(name)})
        except Exception:
            pass                      # on garde l'etat precedent
        out.append(entry)
        _last_state[h] = {'url': entry['url'], 'tabs': entry['tabs']}
    dt = time.time() - t0
    if dt > 1.0:
        print('[uiatabs] passe UIA lente : %.1fs' % dt, flush=True)
    _publish(out)


def _loop():
    import comtypes
    comtypes.CoInitialize()          # une seule fois : l'appartement COM
    while not _stop.is_set():        # doit survivre aux elements caches
        try:
            _fast_pass()
            _uia_pass()
        except Exception as e:
            print('[uiatabs]', e, flush=True)
        _stop.wait(REFRESH_S)


def snapshot():
    """Copie du dernier etat connu : [{'hwnd','proc','title','url','tabs'}]."""
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True).start()
    with _lock:
        return [dict(e, tabs=list(e['tabs'])) for e in _snap]


def select(hwnd, tab_title):
    """Active l'onglet donne d'une fenetre navigateur (retourne bool)."""
    try:
        import comtypes
        import uiautomation as auto
        comtypes.CoInitialize()
        ok = False
        c = auto.ControlFromHandle(hwnd)
        if c is not None:
            tabs_ctrl = _cached(
                hwnd, 'strip',
                lambda: _find_strip(c, _strip_suffix(_win_title(hwnd))))
            if tabs_ctrl is None:
                # juste apres une bascule, le titre peut ne plus matcher :
                # deuxieme chance avec le titre demande
                tabs_ctrl = _cached(
                    hwnd, 'strip',
                    lambda: _find_strip(c, tab_title))
            if tabs_ctrl is not None:
                for tab in tabs_ctrl.GetChildren():
                    if (tab.Name or '').strip() == tab_title:
                        tab.GetSelectionItemPattern().Select()
                        ok = True
                        break
        comtypes.CoUninitialize()
        return ok
    except Exception:
        return False


def fg_tab(hwnd):
    """Onglet actif d'une fenetre : {'title','url','hwnd'} ou None."""
    for e in snapshot():
        if e['hwnd'] == hwnd:
            return {'title': _strip_suffix(e['title']), 'url': e['url'],
                    'hwnd': hwnd}
    return None
