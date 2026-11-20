"""
uiatabs.py — onglets et URL des fenetres Chrome/Edge SANS extension :
lecture de l'arbre d'accessibilite Windows (UI Automation).
- thread de rafraichissement (3 s) : pour chaque fenetre navigateur visible :
  titre, URL (barre d'adresse), liste des onglets, onglet actif
- select(hwnd, titre) : change d'onglet via SelectionItemPattern
Limitations UIA/Chrome : onglets virtualises (hors bande visible) absents ;
l'URL n'est connue que pour l'onglet ACTIF de chaque fenetre.
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
u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

BROWSERS = ('chrome.exe', 'msedge.exe')
REFRESH_S = 1.2            # un scan complet ne prend que ~0,2 s
URL_STICKY_S = 15.0        # garder la derniere URL connue si lecture rate
BROWSER_SUFFIXES = (' - Google Chrome', ' - Microsoft\xad Edge', ' - Mozilla Firefox',
                    ' - Brave', ' - Opera', ' - Vivaldi')

_lock = threading.Lock()
_snap = []                      # [{'hwnd','proc','title','url','tabs':[...] }]
_last_url = {}                  # hwnd -> (url, t) : URL collante anti-flicker
_stop = threading.Event()
_started = False


def _win_title(h):
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(h, buf, 512)
    return buf.value.strip()


def _strip_suffix(title):
    for s in BROWSER_SUFFIXES:
        if title.endswith(s):
            return title[:-len(s)]
    return title


def _find_strip(root, base, budget=0.5):
    """DFS limite : premier TabControl dont un enfant porte le nom de
    l'onglet actif (= bandeau navigateur). None si introuvable."""
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


def _scan():
    """Un passage UIA (thread dedie, COM initialise)."""
    import comtypes
    import uiautomation as auto
    comtypes.CoInitialize()
    import psutil
    out = []
    found = []
    ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    @ENUMPROC
    def cb(h, lp):
        if u32.IsWindowVisible(h) and _win_title(h):
            found.append(h)
        return True

    u32.EnumWindows(cb, 0)
    for h in found:
        try:
            pid = wt.DWORD()
            u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            proc = psutil.Process(pid.value).name().lower()
        except Exception:
            continue
        if proc not in BROWSERS:
            continue
        title = _win_title(h)
        entry = {'hwnd': h, 'proc': proc, 'title': title, 'url': '',
                 'tabs': []}
        try:
            c = auto.ControlFromHandle(h)
            if c is None:
                continue
            # URL de la barre d'adresse (onglet actif) : premier Edit dont
            # la valeur ressemble a une adresse
            edit = c.EditControl(searchDepth=30)
            for _ in range(6):
                if edit is None:
                    break
                try:
                    v = edit.GetValuePattern().Value
                    if v and (v.startswith('http')
                              or ('.' in v and ' ' not in v)):
                        if not v.startswith('http'):
                            v = 'https://' + v   # UIA rend l'URL sans schema
                        entry['url'] = v
                        _last_url[h] = (v, time.time())
                        break
                except Exception:
                    pass
                edit = edit.GetNextSiblingControl()
            if not entry['url']:
                last = _last_url.get(h)
                if last and time.time() - last[1] < URL_STICKY_S:
                    entry['url'] = last[0]    # lecture ratee : derniere connue
            # onglets : le VRAI bandeau est le TabControl qui contient un
            # onglet nomme comme le titre de la fenetre (les pages web ont
            # aussi leurs propres TabControl internes !)
            tabs_ctrl = _find_strip(c, _strip_suffix(title))
            if tabs_ctrl is None:
                tabs_ctrl = c.TabControl(searchDepth=30)   # repli
            if tabs_ctrl is not None:
                base = _strip_suffix(title)
                for tab in tabs_ctrl.GetChildren():
                    name = (tab.Name or '').strip()
                    if not name:
                        continue
                    entry['tabs'].append({
                        'title': name,
                        'active': name == base or base.startswith(name)})
        except Exception:
            continue
        out.append(entry)
    comtypes.CoUninitialize()
    return out


def _loop():
    while not _stop.is_set():
        try:
            snap = _scan()
            with _lock:
                globals()['_snap'] = snap
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
            buf = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, buf, 512)
            tabs_ctrl = _find_strip(c, _strip_suffix(buf.value.strip()))
            if tabs_ctrl is None:
                tabs_ctrl = c.TabControl(searchDepth=30)
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
