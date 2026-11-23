"""
uiatabs.py — onglets et URL des fenetres navigateur SANS extension :
lecture de l'arbre d'accessibilite Windows (UI Automation).
- thread Win32 (0,35 s) : par fenetre navigateur : titre frais
- thread UIA (0,8 s) : URL (barre d'adresse), onglets, onglet actif, et
  `fresh` : True quand l'onglet actif du bandeau UIA porte le titre de la
  fenetre (= l'arbre d'accessibilite est a jour, l'URL est credible)
- select(hwnd, titre) : change d'onglet via SelectionItemPattern
- run_in_uia_thread(fn) : execute fn DANS le thread UIA (l'objet COM global du
  paquet uiautomation appartient a ce thread — un walk depuis un autre thread
  retourne silencieusement vide)
Performances : les elements UIA (barre d'adresse, bandeau d'onglets) sont
MIS EN CACHE par fenetre — ils persistent entre les changements d'onglet ;
les rechercher a chaque scan coute 0,5-1 s ET echoue juste apres une
bascule (le titre de fenetre change AVANT l'arbre UIA).
Limitations UIA/Chrome : onglets virtualises (hors bande) absents ; l'URL
n'est connue que pour l'onglet ACTIF de chaque fenetre.
"""
import ctypes
import re
import threading
import time
from ctypes import wintypes as wt

u32 = ctypes.WinDLL('user32')
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM),
                            wt.LPARAM]
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u32.GetWindowThreadProcessId.restype = wt.DWORD

BROWSERS = ('chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe',
            'opera.exe', 'vivaldi.exe')
FAST_S = 0.35              # thread Win32 pur : titres toujours frais (toujours actif)
UIA_S = 0.8                # thread UIA : URL/onglets, quand une page les affiche
IDLE_S = 3.0               # ... et seulement toutes les 3 s quand elles sont masquées
URL_STICKY_S = 8.0         # garder la derniere URL connue si lecture ratee
# suffixe produit du titre de fenetre ("Page - Google Chrome",
# "Page - Microsoft­ Edge" — le tiret mou est dans le titre d'Edge)
BROWSER_SUFFIX = re.compile(
    r'\s+[-\u2013\u2014]\s+(?:Google Chrome|Microsoft[\xad\u200b ]*Edge'
    r'|Mozilla Firefox|Brave|Opera|Vivaldi)\s*$')

_lock = threading.Lock()
_start_lock = threading.Lock()
_snap = []                      # [{'hwnd','proc','title','url','fresh','tabs':[...] }]
_last_url = {}                  # hwnd -> (url, t) : URL collante anti-flicker
_el_cache = {}                  # hwnd -> {'edit': ctrl, 'strip': ctrl}
_last_state = {}                # hwnd -> {'url','tabs','fresh'} : dernier etat UIA
_proc_cache = {}                # pid -> nom de processus (calcule une fois)
_jobs = []                      # [[fn, queue, annulee]] : taches du thread UIA
_stop = threading.Event()
_started = False
_need = 0                       # pages affichées qui lisent les onglets (CONTEXTE, FENETRES)
_wake = threading.Event()       # acquire() : passe UIA immédiate


def acquire():
    """Une page qui lit les onglets devient courante (fipui.App.hold_tabs) :
    le thread UIA repasse à sa cadence normale, avec une passe immédiate —
    les données ont au plus IDLE_S d'ancienneté à l'entrée de la page."""
    global _need
    with _lock:
        _need += 1
    _wake.set()


def release():
    """La page est quittée : le thread UIA ralentit (IDLE_S) tant qu'aucune
    page affichée ne lit les onglets."""
    global _need
    with _lock:
        _need = max(0, _need - 1)


class UiaTimeout(TimeoutError):
    """La tache n'a pas pu s'executer dans le delai : le thread UIA est occupe
    (walk de page long) ou bloque. Distingue une lecture VIDE d'une lecture
    IMPOSSIBLE."""


def run_in_uia_thread(fn, timeout=8):
    """Execute fn dans le thread UIA dedie. Retourne son resultat ; leve
    UiaTimeout si le thread n'est pas disponible dans `timeout` secondes (la
    tache est alors annulee : elle ne s'executera pas plus tard par surprise)."""
    import queue
    q = queue.Queue()
    job = [fn, q, False]
    _jobs.append(job)
    try:
        r = q.get(timeout=timeout)
    except queue.Empty:
        job[2] = True
        raise UiaTimeout('thread UIA indisponible pendant %s s' % timeout) from None
    if isinstance(r, BaseException):
        raise r                            # l'erreur de fn, a l'appelant
    return r


def _publish(snap):
    with _lock:
        globals()['_snap'] = snap


def _win_title(h):
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(h, buf, 512)
    return buf.value.strip()


def strip_suffix(title):
    """Titre de l'onglet actif : titre de fenetre moins le nom du navigateur."""
    return BROWSER_SUFFIX.sub('', title)


_strip_suffix = strip_suffix           # ancien nom (compatibilite)


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
    """[(hwnd, pid, processus, titre)] des fenetres navigateurs — 100 % Win32,
    donc IMMUNISEE contre les blocages UIA de Chrome. Processus mis en cache
    PAR PID (un hwnd est reutilise par Windows, un pid aussi : le cache est
    purge a chaque passe des entrees qui ne correspondent plus a rien)."""
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
        pid = wt.DWORD()
        if not u32.GetWindowThreadProcessId(h, ctypes.byref(pid)):
            continue
        proc = _proc_cache.get(pid.value)
        if proc is None:
            try:
                import psutil
                proc = psutil.Process(pid.value).name().lower()
            except Exception:
                proc = '?'
            _proc_cache[pid.value] = proc
        if proc in BROWSERS:
            out.append((h, pid.value, proc, _win_title(h)))
    return out


def _purge(live_hwnds, live_pids):
    for cache in (_el_cache, _last_state, _last_url):
        for h in [k for k in cache if k not in live_hwnds]:
            cache.pop(h, None)
    for pid in [k for k in _proc_cache if k not in live_pids]:
        _proc_cache.pop(pid, None)


def _fast_pass():
    """Publication IMMEDIATE : titres frais (Win32) + URL/onglets du dernier
    etat UIA connu. Le contexte cameleon suit le titre sans attendre que
    Chrome daigne rafraichir son arbre d'accessibilite."""
    wins = _browser_windows()
    _purge({w[0] for w in wins}, {w[1] for w in wins})
    snap = []
    for h, _pid, proc, title in wins:
        st = _last_state.get(h) or {}
        snap.append({'hwnd': h, 'proc': proc, 'title': title,
                     'url': st.get('url', ''),
                     'fresh': bool(st.get('fresh')),
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
        entry = dict(ent, tabs=[], fresh=False)
        try:
            c = auto.ControlFromHandle(h)
            if c is not None:
                edit = _cached(h, 'edit', lambda c=c: _find_edit(c))
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
                    h, 'strip', lambda c=c, base=base: _find_strip(c, base))
                if tabs_ctrl is not None:
                    for tab in tabs_ctrl.GetChildren():
                        name = (tab.Name or '').strip()
                        if not name:
                            continue
                        active = name == base or base.startswith(name)
                        entry['tabs'].append({'title': name, 'active': active})
                        if active:
                            # l'onglet actif du bandeau = titre de la fenetre :
                            # l'arbre UIA est a jour (l'URL l'est donc aussi)
                            entry['fresh'] = True
        except Exception:
            pass                      # on garde l'etat precedent
        out.append(entry)
        _last_state[h] = {'url': entry['url'], 'tabs': entry['tabs'],
                          'fresh': entry['fresh']}
    dt = time.time() - t0
    if dt > 1.0:
        print('[uiatabs] passe UIA lente : %.1fs' % dt, flush=True)
    _publish(out)


def _fast_loop():
    """Thread Win32 PUR (aucun COM) : publie les titres frais toutes les
    0,35 s — insensible aux blocages UIA de Chrome (l'autre thread peut
    rester bloque plusieurs secondes, celui-ci ne faiblit jamais). Coût
    négligeable (~0,1 % d'un coeur) : il reste actif page masquée, c'est lui
    qui rend les titres instantanés à l'entrée de CONTEXTE."""
    while not _stop.is_set():
        try:
            _fast_pass()
        except Exception as e:
            print('[uiatabs/fast]', e, flush=True)
        _stop.wait(FAST_S)


def _uia_loop():
    import comtypes
    comtypes.CoInitialize()          # une seule fois : l'appartement COM
    while not _stop.is_set():        # doit survivre aux elements caches
        try:
            _uia_pass()
        except Exception as e:
            print('[uiatabs]', e, flush=True)
        while _jobs:                 # taches deleguees (lecture de pages...)
            fn, q, cancelled = _jobs.pop(0)
            if cancelled:            # l'appelant n'attend plus
                continue
            try:
                q.put(fn())
            except Exception as e:
                q.put(e)
        _wake.clear()
        # page CONTEXTE/FENETRES affichée : cadence normale ; sinon on reste
        # tiède (les données ont au plus IDLE_S) — acquire() réveille aussitôt
        _wake.wait(UIA_S if _need else IDLE_S)


def snapshot():
    """Copie du dernier etat connu :
    [{'hwnd','proc','title','url','fresh','tabs'}]. Demarre les threads au
    premier appel."""
    global _started
    if not _started:
        with _start_lock:
            if not _started:
                _started = True
                threading.Thread(target=_fast_loop, daemon=True,
                                 name='uiatabs-fast').start()
                threading.Thread(target=_uia_loop, daemon=True,
                                 name='uiatabs-uia').start()
    with _lock:
        return [dict(e, tabs=list(e['tabs'])) for e in _snap]


def select(hwnd, tab_title):
    """Active l'onglet donne d'une fenetre navigateur (retourne bool)."""
    try:
        import comtypes
        import uiautomation as auto
        comtypes.CoInitialize()
        try:
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
            return ok
        finally:
            comtypes.CoUninitialize()      # aussi en cas d'exception
    except Exception:
        return False


def fg_tab(hwnd):
    """Onglet actif d'une fenetre navigateur : {'title','url','fresh','hwnd'} ou None."""
    for e in snapshot():
        if e['hwnd'] == hwnd:
            return {'title': _strip_suffix(e['title']), 'url': e['url'],
                    'fresh': e.get('fresh', False), 'hwnd': hwnd}
    return None
