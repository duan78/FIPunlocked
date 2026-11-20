"""
contextuel.py — la page cameleon : le FIP s'adapte a la FENETRE ACTIVE.

Principe : thread de surveillance (1,2 s) -> processus + titre de la fenetre
au premier plan. Le titre d'un navigateur contient celui de l'ONGLET ACTIF
(ex. "duan78/FIPunlocked - Google Chrome") -> on en deduit le contexte.

Contexte GITHUB (onglet d'un repo, valide par gh api) :
  S1 Issues (n)     S2 Pull requests (n)   S3 Actions
  S4 Repo (racine)  S5 Copier l'URL        S6 Copier la cmd clone
  Panneau : etoiles / issues / langage / branche / points des derniers runs /
  PR ouvertes. Rafraichi toutes les 45 s via gh (cache par repo).

Contexte par defaut (toute autre fenetre) :
  S1 Copier le titre    S2 Tuer l'app (double appui)   S3 Minimiser
  molette droite = faire defiler le titre s'il est long.

Extensible : ajouter une entree dans HANDLERS (match() + render + boutons).
"""
import ctypes
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes as wt
from urllib.parse import urlsplit

import charte
import fipui
from fipui import App

CREATE_NO_WINDOW = 0x08000000
POLL_S = 1.2
TTL_S = 45.0
AV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'avatars')

# ---- titres de fenetres navigateur (onglet actif dans le titre) ----
RE_GH_CANONIC = re.compile(r'([\w.-]+)/([\w.-]+)\s*·\s*GitHub')
RE_GH_BROWSER = re.compile(
    r'([\w.-]+)/([\w.-]+)/?\s*[-–—]\s*'
    r'(?:Google Chrome|Microsoft\s*Edge|Mozilla Firefox|Brave|Opera|Vivaldi)\s*$')

u32 = ctypes.WinDLL('user32')
u32.GetForegroundWindow.restype = wt.HWND
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.ShowWindow.restype = wt.BOOL
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wt.UINT]
u32.SetWindowPos.restype = wt.BOOL
u32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = wt.LONG
u32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]


class RECT(ctypes.Structure):
    _fields_ = [('left', wt.LONG), ('top', wt.LONG),
                ('right', wt.LONG), ('bottom', wt.LONG)]


u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x0008
SWP_NOMOVE_NOSIZE = 0x0003        # SWP_NOSIZE | SWP_NOMOVE

K32 = ctypes.WinDLL('kernel32')
K32.GlobalAlloc.restype = ctypes.c_void_p
K32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
K32.GlobalLock.restype = ctypes.c_void_p
K32.GlobalLock.argtypes = [ctypes.c_void_p]
K32.GlobalUnlock.argtypes = [ctypes.c_void_p]
K32.GlobalFree.restype = ctypes.c_void_p
K32.GlobalFree.argtypes = [ctypes.c_void_p]
u32.OpenClipboard.argtypes = [wt.HWND]
u32.OpenClipboard.restype = wt.BOOL
u32.CloseClipboard.restype = wt.BOOL
u32.EmptyClipboard.restype = wt.BOOL
u32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
u32.SetClipboardData.restype = wt.HANDLE

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 2
SW_MINIMIZE = 6

PROTECTED = {'explorer.exe', 'python.exe', 'pythonw.exe', 'dwm.exe',
             'svchost.exe', 'csrss.exe', 'lsass.exe', 'services.exe'}


def clipboard_set(text):
    """Presse-papiers via ctypes (aucune dependance)."""
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        data = text.encode('utf-16-le') + b'\x00\x00'
        h = K32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = K32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        K32.GlobalUnlock(h)
        ok = u32.SetClipboardData(CF_UNICODETEXT, h)
        if not ok:
            K32.GlobalFree(h)
        return bool(ok)
    finally:
        u32.CloseClipboard()


def foreground():
    """(process, titre, hwnd, pid) de la fenetre au premier plan."""
    import psutil
    h = u32.GetForegroundWindow()
    if not h:
        return None, '', 0, 0
    buf = ctypes.create_unicode_buffer(1024)
    u32.GetWindowTextW(h, buf, 1024)
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    try:
        name = psutil.Process(pid.value).name().lower()
    except Exception:
        name = '?'
    return name, buf.value, h, pid.value


def gh(args, timeout=15):
    out = subprocess.run(['gh'] + args, capture_output=True, timeout=timeout,
                         creationflags=CREATE_NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode('utf-8', 'replace').strip()[:80])
    import json
    return json.loads(out.stdout.decode('utf-8', 'replace'))


class ContextuelApp(App):
    name = 'contexte'
    title = 'CONTEXTE'
    show_footer = False

    def __init__(self, daemon):
        super().__init__(daemon)
        self.proc, self.wtitle = '', ''
        self.hwnd, self.pid = 0, 0
        self.repo = None               # 'owner/nom' si onglet GitHub valide
        self.pending = None            # candidate en cours de validation
        self.invalid = set()           # candidats rejetes par gh
        self.cache = {}                # repo -> {'t', 'data', 'prs', 'runs'}
        self.tab = None                # onglet actif (UIA)
        self.kind = 'app'              # github | youtube | site | app
        self.pl = None                 # payload du contexte (repo/url)
        self.tab_since = 0.0           # debut du contexte courant (duree)
        self.yt = {}                   # url youtube -> {'data', 'img'}
        self.fav = {}                  # host -> favicon PIL (ou None)
        self.ghd = {}                  # (repo, kind, n) -> {'t', 'data'}
        self.hfd = {}                  # modele HF -> {'t', 'data'}
        self._busy2 = threading.Lock()     # serialise les fetchs detailles
        self.exp_sel = []              # fichiers selectionnes (explorer)
        self._exp = {'t': 0.0, 'map': {}}   # hwnd -> (chemin, obj, sel)
        self._com_init = False
        self.exp_zoom = False          # apercu image plein ecran
        self._zoom_file = None
        self._player = None            # ffplay (audio local)
        self.scroll = 0
        self.exe = ''                 # chemin de l'exe au premier plan
        self.topmost = False
        self.rect = None              # (w, h) de la fenetre
        self._armed = None            # ('kill'|'restart', t0) double appui
        self._busy = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._watch, daemon=True).start()
        threading.Thread(target=self._fetcher, daemon=True).start()

    # ---------- surveillance de la fenetre active ----------

    def _watch(self):
        last = None
        while not self._stop.is_set():
            proc, title, hwnd, pid = foreground()
            self.proc, self.wtitle = proc, title
            self.hwnd, self.pid = hwnd, pid
            if hwnd:
                try:
                    self.topmost = bool(u32.GetWindowLongW(
                        hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
                except Exception:
                    pass
                rc = RECT()
                if u32.GetWindowRect(hwnd, ctypes.byref(rc)):
                    self.rect = (rc.right - rc.left, rc.bottom - rc.top)
                try:
                    import psutil
                    self.exe = psutil.Process(pid).exe()
                except Exception:
                    self.exe = ''
            kind, pl, self.tab = self._classify(title)
            self.kind, self.pl = kind, pl
            repo = pl if kind == 'github' else None
            if repo and repo in self.invalid:
                repo = None
            if kind == 'youtube' and pl and pl not in self.yt:
                self._yt_fetch(pl)
            if kind == 'site' and pl:
                host = urlsplit(pl).netloc
                if host and host not in self.fav:
                    self._fav_fetch(host)
            if kind == 'ghpage' and isinstance(pl, dict):
                key = (pl['repo'], pl['kind'], pl['n'])
                c = self.ghd.get(key)
                if (not c or time.time() - c['t'] > TTL_S) and \
                        self._busy2.acquire(blocking=False):
                    threading.Thread(target=self._fetch_ghpage,
                                     args=(dict(pl),), daemon=True).start()
            if kind == 'hf' and pl:
                c = self.hfd.get(pl)
                if (not c or time.time() - c['t'] > 60) and \
                        self._busy2.acquire(blocking=False):
                    threading.Thread(target=self._fetch_hf, args=(pl,),
                                     daemon=True).start()
            if kind == 'explorer':
                self.exp_sel = self._explorer_sel()
            changed = (repo, kind, pl, proc, title[:60]) != last
            if pl != getattr(self, '_last_pl', None):
                self._last_pl = pl
                self.tab_since = time.time()      # nouveau contexte = chrono
            if repo != self.repo:
                self.repo = repo
                self.scroll = 0
                if repo and repo not in self.cache:
                    self.pending = repo
                changed = True
            if changed:
                last = (repo, kind, pl, proc, title[:60])
                self.d._redraw_now()
            self._stop.wait(POLL_S)

    @staticmethod
    def _extract_repo(title):
        for rx in (RE_GH_CANONIC, RE_GH_BROWSER):
            m = rx.search(title)
            if m:
                o, r = m.group(1), m.group(2)
                if len(o) >= 2 and len(r) >= 2:
                    return f'{o}/{r}'
        return None

    # ---------- classification du contexte (fenetre/onglet actif) ----------

    def _fg_tab(self):
        """Onglet actif de la fenetre navigateur au premier plan, via UIA
        (uiatabs.py — sans extension) : {'title','url','hwnd'} ou None."""
        if self.proc not in ('chrome.exe', 'msedge.exe') or not self.hwnd:
            return None
        import uiatabs
        try:
            tab = uiatabs.fg_tab(self.hwnd)
        except Exception:
            tab = None
        if tab and tab.get('url'):
            return tab
        return None

    def _classify(self, title):
        """-> (kind, payload, tab) : github+repo | youtube+url | site+url |
        app+None. L'URL de l'onglet (extension) prime sur le titre."""
        tab = self._fg_tab()
        if tab is not None:
            url = tab.get('url') or ''
            host = urlsplit(url).netloc.lower()
            path = urlsplit(url).path
            if host == 'mail.google.com':
                return 'gmail', url, tab
            if host in ('www.google.com', 'google.com', 'www.google.fr',
                        'google.fr') and path == '/search':
                return 'gsearch', url, tab
            if host in ('z.ai', 'chat.z.ai', 'chatgpt.com',
                        'chat.openai.com', 'claude.ai', 'poe.com',
                        'lechat.mistral.ai'):
                return 'chat', url, tab
            if host.endswith('huggingface.co'):
                m = re.match(r'^/([\w.-]+)/([\w.-]+)/?$', path)
                if m and m.group(1) not in (
                        'settings', 'spaces', 'datasets', 'models', 'docs',
                        'blog', 'join', 'login', 'logout', 'signup',
                        'tasks', 'trending', 'papers', 'organizations',
                        'jobs', 'checkout', 'docs'):
                    return 'hf', f'{m.group(1)}/{m.group(2)}', tab
            if 'github.com' in host:
                md = re.match(r'^/([\w.-]+)/([\w.-]+)/(pull|issues)/(\d+)',
                              path)
                if md and md.group(1) not in ('orgs', 'users', 'marketplace'):
                    return 'ghpage', {'repo': f'{md.group(1)}/{md.group(2)}',
                                      'kind': ('pull' if md.group(3) == 'pull'
                                               else 'issue'),
                                      'n': int(md.group(4))}, tab
                m = re.match(r'^/([\w.-]+)/([\w.-]+)', path)
                if m and len(m.group(2)) >= 2 and m.group(1) not in (
                        'orgs', 'users', 'settings', 'marketplace',
                        'topics', 'features', 'explore', 'notifications',
                        'pulls', 'search', 'new', 'codespaces'):
                    return 'github', f'{m.group(1)}/{m.group(2)}', tab
            yt = (('youtube.com' in host and
                   (path == '/watch' or host.startswith('music.')))
                  or 'youtu.be' in host)
            if yt and url:
                return 'youtube', url, tab
            if url.startswith('http'):
                return 'site', url, tab
            return 'app', None, tab
        repo = self._extract_repo(title)      # sans extension : par titre
        if repo:
            return 'github', repo, None
        # ---- applications specifiques (fenetre au premier plan) ----
        if self.proc == 'explorer.exe':
            path = self._explorer_lookup()
            if path is not None:
                return 'explorer', path, None
        elif self.proc == 'code.exe':
            m = re.match(r'^(.*?)\s+-\s+(.*?)\s+-\s+Visual Studio Code\s*$',
                         title)
            if m:
                return 'code', {'file': m.group(1), 'ws': m.group(2)}, None
        elif self.proc in ('windowsterminal.exe', 'wt.exe'):
            m = re.search(r'([A-Za-z]:\\[^\s:;|]+)', title)
            return 'terminal', {'cwd': m.group(1) if m else None}, None
        elif self.proc == 'vlc.exe':
            m = re.match(r'^(.*?)\s+-\s+Lecteur multimédia VLC\s*$', title)
            return 'vlc', {'name': (m.group(1) if m else title)}, None
        return 'app', None, None

    # ---------- explorer : chemins reels via COM Shell.Application ----------

    def _explorer_map(self):
        """{hwnd: (chemin, objet navigateur)} — COM initialisé dans le thread."""
        if not self._com_init:
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception:
                pass
            self._com_init = True
        if time.time() - self._exp['t'] < 1.5:
            return self._exp['map']
        out = {}
        try:
            from comtypes.client import CreateObject
            for w in CreateObject('Shell.Application').Windows():
                try:
                    hwnd = int(w.hwnd)
                    loc = w.LocationURL or ''
                    path = ''
                    if loc.startswith('file:///'):
                        from urllib.parse import unquote
                        path = unquote(loc[8:]).replace('/', os.sep)
                    if not path:
                        path = w.Document.Folder.Self.Path
                    sel = []
                    try:                      # fichiers selectionnes
                        s = w.Document.SelectedItems()
                        for i in range(min(s.Count(), 8)):
                            sel.append(s.Item(i).Path)
                    except Exception:
                        pass
                    if path and not path.startswith('::'):
                        out[hwnd] = (path, w, sel)
                    else:
                        name = w.Document.Folder.Self.Name or 'Dossier'
                        out[hwnd] = (name, w, sel)
                except Exception:
                    continue
        except Exception as e:
            print('[contexte] shell COM:', e, flush=True)
        self._exp = {'t': time.time(), 'map': out}
        return out

    def _explorer_lookup(self):
        """Chemin de la fenetre explorer au premier plan, ou None."""
        try:
            return self._explorer_map().get(self.hwnd,
                                            (None, None, []))[0]
        except Exception:
            return None

    def _explorer_obj(self):
        try:
            return self._explorer_map().get(self.hwnd,
                                            (None, None, []))[1]
        except Exception:
            return None

    def _explorer_sel(self):
        """Chemins des elements selectionnes dans la fenetre au premier plan."""
        try:
            return self._explorer_map().get(self.hwnd, (None, None, []))[2]
        except Exception:
            return []

    @staticmethod
    def _unix_path(p):
        """C:\\Users\\x -> /c/Users/x (git bash)."""
        if not p:
            return p
        p = p.replace('\\', '/')
        if len(p) >= 2 and p[1] == ':':
            return '/' + p[0].lower() + p[2:]
        return p

    def _yt_fetch(self, url):
        """Metadonnees + miniature YouTube via oEmbed (sans cle)."""
        import io as _io
        entry = {'data': None, 'img': None}
        self.yt[url] = entry
        try:
            import requests
            r = requests.get('https://www.youtube.com/oembed',
                             params={'url': url, 'format': 'json'},
                             timeout=6)
            if r.status_code == 200:
                entry['data'] = r.json()
                ir = requests.get(entry['data'].get('thumbnail_url'),
                                  timeout=6)
                if ir.status_code == 200:
                    from PIL import Image
                    entry['img'] = Image.open(_io.BytesIO(ir.content)) \
                        .convert('RGB')
        except Exception:
            pass
        self.d._redraw_now()

    def _fav_fetch(self, host):
        """Favicon via le service Google S2 (cache par host)."""
        import io as _io
        self.fav[host] = None
        try:
            import requests
            r = requests.get('https://www.google.com/s2/favicons',
                             params={'domain': host, 'sz': 64}, timeout=6)
            if r.status_code == 200 and r.content[:4] != b'None':
                from PIL import Image
                img = Image.open(_io.BytesIO(r.content)).convert('RGBA')
                self.fav[host] = img
        except Exception:
            pass
        self.d._redraw_now()

    def _fetch_ghpage(self, pl):
        """PR ou issue GitHub : gh pr/issue view (cache TTL 45 s)."""
        key = (pl['repo'], pl['kind'], pl['n'])
        try:
            if pl['kind'] == 'pull':
                data = gh(['pr', 'view', str(pl['n']), '-R', pl['repo'],
                           '--json', 'title,author,state,additions,deletions,'
                           'comments,reviews,createdAt,url'])
            else:
                data = gh(['issue', 'view', str(pl['n']), '-R', pl['repo'],
                           '--json',
                           'title,author,state,comments,createdAt,url,labels'])
            self.ghd[key] = {'t': time.time(), 'data': data}
        except Exception as e:
            self.ghd[key] = {'t': time.time(), 'data': None,
                             'err': str(e).split('\n')[0][:70]}
        finally:
            self._busy2.release()
            self.d._redraw_now()

    def _fetch_hf(self, model):
        """Modele HuggingFace : API publique (cache TTL 60 s)."""
        try:
            import requests
            r = requests.get(f'https://huggingface.co/api/models/{model}',
                             timeout=8)
            r.raise_for_status()
            self.hfd[model] = {'t': time.time(), 'data': r.json()}
        except Exception as e:
            self.hfd[model] = {'t': time.time(), 'data': None,
                               'err': str(e)[:70]}
        finally:
            self._busy2.release()
            self.d._redraw_now()

    # ---------- recuperation des donnees GitHub (thread fetcher) ----------

    def _fetcher(self):
        while not self._stop.is_set():
            repo = self.repo or self.pending
            if repo:
                c = self.cache.get(repo)
                fresh = c and time.time() - c['t'] < TTL_S
                if not fresh and self._busy.acquire(blocking=False):
                    threading.Thread(target=self._fetch, args=(repo,),
                                     daemon=True).start()
            self._stop.wait(1.5)

    def _fetch(self, repo):
        try:
            data = gh(['api', f'repos/{repo}'])
            full = data['full_name']
            try:
                prs = gh(['pr', 'list', '-R', full, '--state', 'open',
                          '--limit', '3', '--json', 'number,title,author'])
            except Exception:
                prs = []
            try:
                runs = gh(['run', 'list', '-R', full, '--limit', '3',
                           '--json', 'status,conclusion,workflowName'])
            except Exception:
                runs = []
            self.cache[repo] = {'t': time.time(), 'data': data,
                                'prs': prs, 'runs': runs,
                                'avatar': self._fetch_avatar(data)}
            self.cache[full] = self.cache[repo]      # alias casse differente
        except Exception as e:
            self.invalid.add(repo)                   # pas un repo (ou echec)
            if self.repo == repo:
                self.repo = None
            print(f'[contexte] {repo} invalide: {e}', flush=True)
        finally:
            if self.pending == repo:
                self.pending = None
            self._busy.release()
            self.d._redraw_now()

    # ---------- boutons ----------

    def on_event(self, name, pressed):
        if not pressed:
            return
        import webbrowser
        if name == 'right_cw':
            self.scroll += 1
            return
        if name == 'right_ccw':
            self.scroll = max(0, self.scroll - 1)
            return
        if name not in ('S1', 'S2', 'S3', 'S4', 'S5', 'S6'):
            return
        n = int(name[1])
        tab = self.tab or {}
        if self.kind == 'gsearch' and self.pl:
            import keysend
            from urllib.parse import parse_qs as _pqs, quote_plus as _qp
            q = (_pqs(urlsplit(self.pl).query).get('q') or [''])[0]
            if n == 1:
                clipboard_set(q)
                self.alert('requete copiee')
            elif n == 2:                      # I'm feeling lucky
                os.startfile('https://www.google.com/search?q=' + _qp(q)
                             + '&btnI')
                self.alert('lucky...')
            elif n == 3:
                os.startfile('https://www.google.com/search?q=' + _qp(q)
                             + '&tbm=isch')
                self.alert('images')
            elif n == 4:
                os.startfile('https://www.google.com/search?q=' + _qp(q)
                             + '&tbm=nws')
                self.alert('actualites')
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'chat' and self.pl:
            import keysend
            host = urlsplit(self.pl).netloc
            if n == 1:
                clipboard_set(tab.get('title') or '')
                self.alert('titre copie')
            elif n == 2:
                clipboard_set(self.pl)
                self.alert('URL copiee')
            elif n == 3:                      # nouvelle conversation
                os.startfile('https://' + host + '/')
                self.alert('accueil ' + host)
            elif n == 4:
                webbrowser.open('https://www.google.com/search?q=' +
                                (tab.get('title') or host)
                                .replace(' ', '+'))
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'gmail':
            import keysend
            m2 = re.search(r'-\s*([\w.+-]+@[\w.-]+)', tab.get('title') or '')
            if n == 1:                        # nouveau message
                os.startfile('https://mail.google.com/mail/u/0/#inbox'
                             '?compose=new')
                self.alert('nouveau message')
            elif n == 2 and m2:
                clipboard_set(m2.group(1))
                self.alert('adresse copiee')
            elif n == 3:
                webbrowser.open('https://www.google.com/search?q=' +
                                (tab.get('title') or 'gmail')
                                .replace(' ', '+'))
            elif n == 4:
                clipboard_set(self.pl or '')
                self.alert('URL copiee')
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'ghpage' and isinstance(self.pl, dict):
            import keysend
            pl = self.pl
            key = (pl['repo'], pl['kind'], pl['n'])
            data = (self.ghd.get(key) or {}).get('data') or {}
            base = 'https://github.com/{}/{}/{}'.format(
                pl['repo'], 'pull' if pl['kind'] == 'pull' else 'issues',
                pl['n'])
            if n == 1:
                clipboard_set(base)
                self.alert('lien copie')
            elif n == 2:
                clipboard_set(data.get('title', ''))
                self.alert('titre copie')
            elif n == 3:
                webbrowser.open('https://www.google.com/search?q=' +
                                (data.get('title') or
                                 '{} #{}'.format(pl['repo'], pl['n']))
                                .replace(' ', '+'))
            elif n == 4:
                clipboard_set(base + ('/files' if pl['kind'] == 'pull'
                                      else ''))
                self.alert('lien ' + ('fichiers' if pl['kind'] == 'pull'
                                      else 'discussion'))
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'hf' and self.pl:
            import keysend
            model = self.pl
            if n == 1:
                clipboard_set(model)
                self.alert('ID copie')
            elif n == 2:
                clipboard_set('https://huggingface.co/' + model)
                self.alert('page copiee')
            elif n == 3:
                clipboard_set('huggingface-cli download ' + model)
                self.alert('commande copiee')
            elif n == 4:
                webbrowser.open('https://www.google.com/search?q=' +
                                model.replace('/', ' ').replace(' ', '+'))
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'youtube' and self.pl:
            import keysend
            e = self.yt.get(self.pl) or {}
            data = e.get('data') or {}
            title = (data.get('title')
                     or tab.get('title', '').replace(' - YouTube', ''))
            if n == 1:
                keysend.tap('playpause')
            elif n == 2:
                clipboard_set(self.pl)
                self.alert('URL copiee')
            elif n == 3:
                clipboard_set(title)
                self.alert('titre copie')
            elif n == 4:
                webbrowser.open('https://www.google.com/search?q=' +
                                (title or '').replace(' ', '+'))
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'site' and self.pl:
            import keysend
            if n == 1:
                clipboard_set(self.pl)
                self.alert('URL copiee')
            elif n == 2:
                clipboard_set(tab.get('title') or '')
                self.alert('titre copie')
            elif n == 3:
                webbrowser.open('https://www.google.com/search?q=' +
                                (tab.get('title') or self.pl)
                                .replace(' ', '+'))
            elif n == 4:
                clipboard_set(urlsplit(self.pl).netloc)
                self.alert('domaine copie')
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'explorer' and self.pl:
            path = self.pl
            sel = self.exp_sel or []
            single = self._sel_single()
            fk = self._filekind(single) if single else None
            if fk:                             # --- selection unique typee
                if n == 1:
                    clipboard_set(single)
                    self.alert('chemin copie')
                elif n == 2:
                    clipboard_set(self._unix_path(single))
                    self.alert('unix copie')
                elif n == 3:
                    if fk == 'image':          # zoom plein ecran
                        self.exp_zoom = not self.exp_zoom
                        self._zoom_file = single if self.exp_zoom else None
                    elif fk == 'video':        # lecture SUR LE FIP
                        threading.Thread(
                            target=lambda: self.d.play_video(single),
                            daemon=True).start()
                        self.alert('video sur le FIP')
                    elif fk == 'audio':        # ffplay (stop si en cours)
                        if self._player is not None and \
                                self._player.poll() is None:
                            self._player.terminate()
                            self._player = None
                            self.alert('arret audio')
                        else:
                            import shutil
                            ff = shutil.which('ffplay')
                            if ff:
                                self._player = subprocess.Popen(
                                    [ff, '-nodisp', '-autoexit',
                                     '-loglevel', 'quiet', single])
                                self.alert('lecture audio')
                            else:
                                self.alert('ffplay introuvable')
                    elif fk == 'py':           # executer dans un terminal
                        try:
                            subprocess.Popen(['wt', 'python', single])
                        except FileNotFoundError:
                            subprocess.Popen(['cmd', '/c', 'start', 'cmd',
                                              '/k', 'python', single])
                        self.alert('execution...')
                    elif fk == 'zip':
                        try:
                            import zipfile
                            with zipfile.ZipFile(single) as z:
                                self.alert('{} fichiers'.format(
                                    len(z.infolist())))
                        except Exception as e:
                            self.alert(str(e)[:24])
                    else:                      # autre : dossier parent
                        parent = os.path.dirname(path.rstrip('\\/'))
                        if parent and parent != path:
                            os.startfile(parent)
                            self.alert('dossier parent')
                elif n == 4:                   # VS Code
                    try:
                        subprocess.Popen(['code', single])
                        self.alert('VS Code')
                    except FileNotFoundError:
                        self.alert('code absent du PATH')
                elif n == 5:                   # terminal du dossier
                    try:
                        subprocess.Popen(['wt', '-d', path])
                    except FileNotFoundError:
                        subprocess.Popen(['cmd', '/c', 'start', 'cmd', '/k',
                                          'cd', '/d', path])
                    self.alert('terminal ouvert')
                elif n == 6:
                    w = self._explorer_obj()
                    if w is not None:
                        try:
                            w.Refresh()
                            self.alert('actualise')
                        except Exception:
                            pass
            else:                               # --- dossier / multi
                fpath = sel[0] if sel else path
                if n == 1:
                    clipboard_set(fpath)
                    self.alert('chemin copie')
                elif n == 2:
                    try:
                        subprocess.Popen(['wt', '-d', path])
                    except FileNotFoundError:
                        subprocess.Popen(['cmd', '/c', 'start', 'cmd', '/k',
                                          'cd', '/d', path])
                    self.alert('terminal ouvert')
                elif n == 3:
                    clipboard_set(self._unix_path(fpath))
                    self.alert('chemin unix copie')
                elif n == 4:
                    parent = os.path.dirname(path.rstrip('\\/'))
                    if parent and parent != path:
                        w = self._explorer_obj()
                        done = False
                        if w is not None:
                            try:
                                w.Navigate2(parent)
                                done = True
                            except Exception:
                                pass
                        if not done:
                            os.startfile(parent)
                        self.alert('dossier parent')
                    else:
                        self.alert('deja a la racine')
                elif n == 5:
                    target = sel[0] if sel else path
                    try:
                        subprocess.Popen(['code', target])
                        self.alert('VS Code')
                    except FileNotFoundError:
                        self.alert('code absent du PATH')
                elif n == 6:
                    w = self._explorer_obj()
                    if w is not None:
                        try:
                            w.Refresh()
                            self.alert('actualise')
                        except Exception:
                            pass
        elif self.kind == 'code' and isinstance(self.pl, dict):
            import keysend
            if n == 1:
                clipboard_set(self.pl.get('file', ''))
                self.alert('fichier copie')
            elif n == 2:
                clipboard_set(self.pl.get('ws', ''))
                self.alert('workspace copie')
            elif n == 3:
                webbrowser.open('https://www.google.com/search?q=' +
                                (self.pl.get('file', '') + ' '
                                 + os.path.splitext(
                                     self.pl.get('file', ''))[1])
                                .strip().replace(' ', '+'))
            elif n == 4 and self.hwnd:         # toujours devant
                on = not self.topmost
                if u32.SetWindowPos(self.hwnd, wt.HWND(-1 if on else -2),
                                     0, 0, 0, 0, SWP_NOMOVE_NOSIZE):
                    self.topmost = on
                    self.alert('toujours devant: ' + ('OUI' if on else 'NON'))
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'terminal' and isinstance(self.pl, dict):
            import keysend
            cwd = self.pl.get('cwd')
            if n == 1 and cwd:
                clipboard_set(cwd)
                self.alert('chemin copie')
            elif n == 2 and cwd:
                os.startfile(cwd)
                self.alert('explorateur ouvert')
            elif n == 3 and cwd:
                clipboard_set(self._unix_path(cwd))
                self.alert('chemin unix copie')
            elif n == 4:
                webbrowser.open('https://www.google.com/search?q=' +
                                self.wtitle.replace(' ', '+'))
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.kind == 'vlc':
            import keysend
            if n == 1:
                keysend.tap('space')
            elif n == 2:
                keysend.tap('s')
            elif n == 3:
                clipboard_set((self.pl or {}).get('name', ''))
                self.alert('titre copie')
            elif n == 4:
                keysend.tap('m')
            elif n == 5:
                keysend.tap('volumedown')
            elif n == 6:
                keysend.tap('volumeup')
        elif self.repo:
            base = f'https://github.com/{self.repo}'
            if n == 1:
                webbrowser.open(base + '/issues')
            elif n == 2:
                webbrowser.open(base + '/pulls')
            elif n == 3:
                webbrowser.open(base + '/actions')
            elif n == 4:
                webbrowser.open(base)
            elif n == 5:
                clipboard_set(base)
                self.alert('URL copiee')
            elif n == 6:
                clipboard_set(f'gh repo clone {self.repo}')
                self.alert('cmd clone copiee')
        else:
            if n == 1:
                clipboard_set(self.wtitle)
                self.alert('titre copie')
            elif n == 2 and self.hwnd:             # toujours au premier plan
                on = not self.topmost
                if u32.SetWindowPos(self.hwnd, wt.HWND(-1 if on else -2),
                                     0, 0, 0, 0, SWP_NOMOVE_NOSIZE):
                    self.topmost = on
                    self.alert('toujours devant: ' + ('OUI' if on else 'NON'))
                else:
                    self.alert('refuse par Windows')
            elif n == 3 and self.hwnd:
                u32.ShowWindow(self.hwnd, SW_MINIMIZE)
                self.alert('minimisee')
            elif n == 4:                           # tuer, double appui
                if self._confirm('kill'):
                    self._do_kill()
            elif n == 5:                           # redemarrer, double appui
                if self._confirm('restart'):
                    self._do_restart()
            elif n == 6:
                clipboard_set(self.exe or '?')
                self.alert('chemin exe copie' if self.exe else 'exe inconnu')

    def _confirm(self, action):
        """Double appui : 1er appui arme (retourne False), 2e = confirme."""
        now = time.time()
        if self._armed and self._armed[0] == action and now - self._armed[1] < 3.0:
            self._armed = None
            return True
        self._armed = (action, now)
        self.alert('re-appuyez pour ' + action)
        return False

    def _do_kill(self):
        if not self.pid:
            return
        if self.proc in PROTECTED:
            self.alert('protege: ' + self.proc)
            return
        try:
            import psutil
            psutil.Process(self.pid).kill()
            self.alert(self.proc + ' arrete')
        except Exception as e:
            self.alert(str(e)[:24])

    def _do_restart(self):
        if not self.exe:
            self.alert('exe inconnu')
            return
        if self.proc in PROTECTED:
            self.alert('protege: ' + self.proc)
            return
        try:
            import psutil
            psutil.Process(self.pid).kill()
        except Exception as e:
            self.alert(str(e)[:24])
            return

        def go():
            time.sleep(1.2)
            try:
                subprocess.Popen([self.exe], cwd=os.path.dirname(self.exe))
            except Exception as e:
                print('[contexte] relance:', e, flush=True)
        threading.Thread(target=go, daemon=True).start()
        self.alert('redemarrage de ' + self.proc)

    def http_get(self):
        c = self.cache.get(self.repo) if self.repo else None
        return {'process': self.proc, 'title': self.wtitle[:100],
                'kind': self.kind, 'payload': repr(self.pl)[:120],
                'repo': self.repo, 'pending': self.pending,
                'stars': c['data'].get('stargazers_count') if c else None,
                'issues': c['data'].get('open_issues_count') if c else None,
                'prs': len(c['prs']) if c else 0}

    def _fetch_avatar(self, data):
        """Avatar du proprietaire (cache disque apps/avatars/), 44 px."""
        try:
            owner = data['owner']['login']
            url = data['owner']['avatar_url']
            os.makedirs(AV_DIR, exist_ok=True)
            path = os.path.join(AV_DIR, re.sub(r'[^\w.-]', '_', owner) + '.png')
            if not os.path.exists(path):
                import requests
                r = requests.get(url, timeout=8)
                r.raise_for_status()
                with open(path, 'wb') as f:
                    f.write(r.content)
            from PIL import Image
            return Image.open(path).convert('RGB').resize((44, 44))
        except Exception:
            return None

    @staticmethod
    def _calc(expr):
        """Calculatrice sure (AST, aucune evaluation libre). None si non calc."""
        import ast
        import operator
        expr = (expr.replace('^', '**').replace('×', '*')
                .replace('÷', '/').replace(',', '.'))
        if not re.fullmatch(r'[\d\s+\-*/().%]+', expr or ''):
            return None
        ops = {ast.Add: operator.add, ast.Sub: operator.sub,
               ast.Mult: operator.mul, ast.Div: operator.truediv,
               ast.Pow: operator.pow, ast.Mod: operator.mod,
               ast.USub: operator.neg, ast.UAdd: operator.pos}

        def ev(n):
            if isinstance(n, ast.Constant) and \
                    isinstance(n.value, (int, float)):
                return n.value
            if isinstance(n, ast.BinOp) and type(n.op) in ops:
                return ops[type(n.op)](ev(n.left), ev(n.right))
            if isinstance(n, ast.UnaryOp) and type(n.op) in ops:
                return ops[type(n.op)](ev(n.operand))
            raise ValueError('nope')

        try:
            return round(ev(ast.parse(expr, mode='eval').body), 8)
        except Exception:
            return None

    @staticmethod
    def _fsize(n):
        if n >= 2**30:
            return '{:.1f} Go'.format(n / 2**30)
        if n >= 2**20:
            return '{:.1f} Mo'.format(n / 2**20)
        if n >= 1024:
            return '{:.0f} Ko'.format(n / 1024)
        return '{} o'.format(n)

    @staticmethod
    def _filekind(f):
        """Type contextuel d'un fichier (actions dediees par extension)."""
        ext = os.path.splitext(f)[1].lower()
        if ext in ('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.gif'):
            return 'image'
        if ext in ('.mp4', '.mkv', '.webm', '.avi', '.mov'):
            return 'video'
        if ext in ('.mp3', '.flac', '.wav', '.ogg', '.m4a', '.opus'):
            return 'audio'
        if ext == '.py':
            return 'py'
        if ext == '.zip':
            return 'zip'
        return 'autre'

    @staticmethod
    def _mtime_h(path):
        m = int((time.time() - os.path.getmtime(path)) / 60)
        if m < 60:
            return '{} min'.format(m)
        if m < 2880:
            return '{} h'.format(m // 60)
        return '{} j'.format(m // 1440)

    def _sel_single(self):
        sel = self.exp_sel or []
        if len(sel) == 1 and os.path.isfile(sel[0]):
            return sel[0]
        return None

    def on_leave(self):
        if self._player is not None and self._player.poll() is None:
            try:
                self._player.terminate()
            except Exception:
                pass

    def _tab_pos(self):
        """(index_actif, total) des onglets de la fenetre au premier plan."""
        if not self.hwnd:
            return None, 0
        try:
            import uiatabs
            for e in uiatabs.snapshot():
                if e['hwnd'] == self.hwnd:
                    for i, t in enumerate(e['tabs']):
                        if t['active']:
                            return i + 1, len(e['tabs'])
                    return None, len(e['tabs'])
        except Exception:
            pass
        return None, 0

    def _duree(self):
        s = int(time.time() - self.tab_since) if self.tab_since else 0
        if s < 60:
            return '{} s'.format(s)
        if s < 3600:
            return '{} min'.format(s // 60)
        return '{} h{:02d}'.format(s // 3600, (s % 3600) // 60)

    # ---------- rendu (charte graphique) ----------

    def render_full(self, img):
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, 319, 239), fill=charte.BG)
        if self.kind == 'github' and self.repo:
            self._rg_github(img, d)
        elif self.kind == 'youtube':
            self._rg_youtube(img, d)
        elif self.kind == 'site':
            self._rg_site(img, d)
        elif self.kind == 'explorer':
            if self.exp_zoom and self._zoom_file:
                self._rg_zoom(img, d)
            else:
                self._rg_explorer(img, d)
        elif self.kind == 'code':
            self._rg_code(img, d)
        elif self.kind == 'terminal':
            self._rg_terminal(img, d)
        elif self.kind == 'vlc':
            self._rg_vlc(img, d)
        elif self.kind == 'gmail':
            self._rg_gmail(img, d)
        elif self.kind == 'ghpage':
            self._rg_ghpage(img, d)
        elif self.kind == 'hf':
            self._rg_hf(img, d)
        elif self.kind == 'gsearch':
            self._rg_gsearch(img, d)
        elif self.kind == 'chat':
            self._rg_chat(img, d)
        else:
            self._rg_default(img, d)

    def _chip(self, d, x1, y, text, col):
        f = fipui.font(12)
        w = int(d.textlength(text, font=f)) + 12
        d.rounded_rectangle((x1 - w, y, x1, y + 15), radius=7,
                            fill=charte.CARD, outline=col)
        d.text((x1 - w / 2, y + 8), text, font=f, fill=col, anchor='mm')

    def _wrap(self, d, text, width, font):
        """Decoupe en lignes <= width px, avec '...' sur la derniere coupe."""
        lines, cur = [], ''
        for word in (text or '').split():
            t = (cur + ' ' + word).strip()
            if d.textlength(t, font=font) <= width:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines or [' ']

    def _rg_github(self, img, d):
        if not self.repo:                    # course avec le thread watch
            return self._rg_default(img, d)
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        c = self.cache.get(self.repo)
        data = c['data'] if c else {}
        prs = c['prs'] if c else []
        runs = c['runs'] if c else []
        # ---- colonne GAUCHE (~30%) : 6 keycaps pleine hauteur, alignes S1-S6
        caps = [
            (charte.ico_issue, 'Issues',
             str(data.get('open_issues_count', '')) if c else '', None),
            (charte.ico_branch, 'Pull r.', str(len(prs)) if c else '',
             charte.PURPLE if prs else None),
            (charte.ico_play, 'Actions', '', None),
            (charte.ico_home, 'Repo', '', None),
            (charte.ico_link, 'URL', '', None),
            (charte.ico_terminal, 'Clone', '', None),
        ]
        for i, (ic, lab, val, col) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          h=35, x0=4, x1=94, fsize=11, r=4)
        # ---- colonne DROITE (~70%) : panneau d'informations
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'GH', charte.BLUE)
        av = (c or {}).get('avatar') or charte.letter_avatar(
            self.repo.split('/')[0], 40, 9)
        img.paste(av, (106, 12), charte.rounded(av, 9))
        name = self.repo
        while name and d.textlength(name + '...' if name != self.repo else
                                    name, font=F_S) > 155:
            name = name[:-1]
        if name != self.repo:
            name += '...'
        d.text((154, 22), name, font=F_S, fill=charte.FG, anchor='lm')
        if not c:
            d.text((154, 44), 'chargement gh...', font=F_XS, fill=charte.DIM,
                   anchor='lm')
            return
        lang = data.get('language')
        if lang:
            d.ellipse((154, 39, 162, 47), fill=charte.lang_color(lang))
            d.text((166, 43), lang, font=F_XS, fill=charte.DIM, anchor='lm')
        d.text((106, 64), 'branche ' + data.get('default_branch', '?'),
               font=F_XS, fill=charte.DIM, anchor='lm')
        charte.ico_clock(d, 111, 82, 4, charte.DIM)
        d.text((120, 82), 'push ' + (charte.ago(data.get('pushed_at')) or '?'),
               font=F_XS, fill=charte.DIM, anchor='lm')
        # stats en pills
        x = 106
        x += charte.pill(d, x, 106, charte.ico_star, str(
            data.get('stargazers_count', 0)), charte.ORANGE) + 6
        x += charte.pill(d, x, 106, charte.ico_issue, str(
            data.get('open_issues_count', 0)), charte.GREEN) + 6
        charte.pill(d, x, 106, charte.ico_branch, str(len(prs)),
                    charte.PURPLE)
        # derniers runs
        if runs:
            dx = 106
            for run in runs[:3]:
                col = charte.BLUE if run['status'] != 'completed' else (
                    charte.GREEN if run.get('conclusion') == 'success'
                    else charte.RED if run.get('conclusion')
                    else (120, 125, 140))
                d.ellipse((dx, 126, dx + 11, 137), fill=col)
                dx += 17
            st = runs[0]
            verdict = 'EN COURS' if st['status'] != 'completed' else \
                ('OK' if st.get('conclusion') == 'success' else
                 'ECHEC' if st.get('conclusion') else '?')
            d.text((311, 131), verdict, font=F_XS,
                   fill=charte.BLUE if verdict == 'EN COURS' else
                   (charte.GREEN if verdict == 'OK' else charte.RED),
                   anchor='rm')
        # workflow
        wy = 150
        for ln in self._wrap(d, runs[0].get('workflowName', '-') if runs
                             else 'aucun run', 200, F_T)[:2]:
            d.text((106, wy), ln, font=F_T, fill=charte.DIM, anchor='lm')
            wy += 13
        # PR ouverte
        py = 182
        if prs:
            pr = prs[0]
            d.text((106, py), '#{}'.format(pr['number']), font=F_XS,
                   fill=charte.PURPLE, anchor='lm')
            t = pr['title']
            while t and d.textlength(t + '...', font=F_XS) > 170:
                t = t[:-1]
            d.text((138, py), t + '...', font=F_XS, fill=charte.FG,
                   anchor='lm')
        else:
            d.text((106, py), 'aucune PR ouverte', font=F_T, fill=charte.DIM,
                   anchor='lm')
        # description du repo
        dy = 202
        for ln in self._wrap(d, data.get('description') or '', 200,
                             F_T)[:3]:
            d.text((106, dy), ln, font=F_T, fill=(110, 118, 129),
                   anchor='lm')
            dy += 12

    def _rg_youtube(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        e = self.yt.get(self.pl) or {}
        data = e.get('data') or {}
        title = (data.get('title')
                 or (self.tab or {}).get('title', '').replace(' - YouTube', '')
                 or '(video)')
        caps = [
            (charte.ico_play, 'Lecture', '', None, False),
            (charte.ico_link, 'URL', '', None, False),
            (charte.ico_copy, 'Titre', '', None, False),
            (charte.ico_home, 'Google', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'YT', charte.RED)
        thumb = e.get('img')
        if thumb is not None:
            w, h = 196, 110
            r = max(w / thumb.width, h / thumb.height)
            t2 = thumb.resize((int(thumb.width * r) + 1,
                               int(thumb.height * r) + 1))
            x = (t2.width - w) // 2
            y = (t2.height - h) // 2
            crop = charte.rounded(t2.crop((x, y, x + w, y + h)), 8)
            img.paste(crop, (110, 12), crop)
        else:
            d.rounded_rectangle((110, 12, 306, 122), radius=8,
                                fill=charte.CARD, outline=charte.BORDER)
            d.text((208, 67), 'miniature...', font=F_XS, fill=charte.DIM,
                   anchor='mm')
        import textwrap
        lines = textwrap.wrap(title, 40)[:2] or [' ']
        yy = 138
        for ln in lines:
            d.text((106, yy), ln, font=F_XS, fill=charte.FG, anchor='lm')
            yy += 17
        if data.get('author_name'):
            d.text((106, yy + 4), data['author_name'], font=F_T,
                   fill=charte.DIM, anchor='lm')
        pos, tot = self._tab_pos()
        if pos:
            charte.ico_window(d, 111, yy + 24, 4, charte.DIM)
            d.text((120, yy + 24), 'onglet {}/{}  ·  {}'.format(
                pos, tot, self._duree()), font=F_T, fill=charte.DIM,
                anchor='lm')

    def _rg_site(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        tab = self.tab or {}
        u = urlsplit(self.pl or '')
        host = u.netloc
        caps = [
            (charte.ico_link, 'URL', '', None, False),
            (charte.ico_copy, 'Titre', '', None, False),
            (charte.ico_copy, 'Domaine', '', None, False),
            (charte.ico_home, 'Google', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'WEB', charte.TEAL)
        # en-tete : favicon + domaine (comme avatar + repo de la fiche GitHub)
        fav = self.fav.get(host)
        if fav is not None:
            f2 = fav.resize((36, 36))
            img.paste(f2, (106, 12), f2)
        else:
            av = charte.letter_avatar(host, 36, 8)
            img.paste(av, (106, 12), charte.rounded(av, 8))
        h2 = host.replace('www.', '')
        while h2 and d.textlength(h2, font=F_S) > 150:
            h2 = h2[:-1]
        d.text((152, 24), h2, font=F_S, fill=charte.FG, anchor='lm')
        pos, tot = self._tab_pos()
        charte.ico_clock(d, 157, 45, 4, charte.DIM)
        meta = self._duree()
        if pos:
            meta += '   onglet {}/{}'.format(pos, tot)
        d.text((166, 45), meta, font=F_T, fill=charte.DIM, anchor='lm')
        # zone URL
        d.line((106, 62, 313, 62), fill=charte.BORDER)
        d.text((106, 76), 'URL', font=F_T, fill=charte.DIM, anchor='lm')
        v = self.pl or ''
        while v and d.textlength(v, font=F_T) > 165:
            v = v[:-1]
        d.text((140, 76), v, font=F_T, fill=(120, 160, 230), anchor='lm')
        # zone titre (grand, comme le nom du repo)
        d.line((106, 92, 313, 92), fill=charte.BORDER)
        d.text((106, 104), 'TITRE', font=F_T, fill=charte.DIM, anchor='lm')
        import textwrap
        lines = textwrap.wrap(tab.get('title') or host, 34)[:4] or [' ']
        yy = 124
        for ln in lines:
            d.text((106, yy), ln, font=F_XS, fill=charte.FG, anchor='lm')
            yy += 17
        # bas de panneau : chemin de l'URL
        d.line((106, 196, 313, 196), fill=charte.BORDER)
        path = (u.path + ('?' + u.query[:24] if u.query else '')) or '/'
        while path and d.textlength(path, font=F_T) > 200:
            path = path[:-1]
        d.text((106, 210), path, font=F_T, fill=(110, 118, 129), anchor='lm')
        d.text((106, 226), 'fenetre: ' + self.proc, font=F_T,
               fill=(90, 95, 110), anchor='lm')

    def _rg_zoom(self, img, d):
        """Apercu plein ecran de l'image selectionnee (S3 = retour)."""
        from PIL import Image as PILImage, ImageDraw
        try:
            im = PILImage.open(self._zoom_file).convert('RGB')
        except Exception:
            self.exp_zoom = False
            return self._rg_explorer(img, d)
        r = max(320 / im.width, 240 / im.height)
        im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1))
        x, y = (im.width - 320) // 2, (im.height - 240) // 2
        img.paste(im.crop((x, y, x + 320, y + 240)), (0, 0))
        d = ImageDraw.Draw(img, 'RGBA')
        d.rectangle((0, 0, 319, 20), fill=(0, 0, 0, 150))
        d.text((8, 10), os.path.basename(self._zoom_file)[:36],
               font=fipui.font(13), fill=(255, 255, 255), anchor='lm')
        try:
            d.text((312, 10), self._fsize(os.path.getsize(self._zoom_file)),
                   font=fipui.font(13), fill=(255, 255, 255), anchor='rm')
        except OSError:
            pass

    def _rg_explorer(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        path = self.pl or '?'
        sel = self.exp_sel or []
        single = self._sel_single()
        fk = self._filekind(single) if single else None
        if single != self._zoom_file:
            self._zoom_file = None
            self.exp_zoom = False
        is_dir = os.path.isdir(path)
        name = os.path.basename(path.rstrip('\\/')) or path
        # keycaps contextuels : par type de fichier si selection unique
        if fk:
            playing = self._player is not None and self._player.poll() is None
            act3 = {
                'image': (charte.ico_window, 'Zoom',
                          'ON' if self.exp_zoom else '',
                          charte.BLUE if self.exp_zoom else None,
                          self.exp_zoom),
                'video': (charte.ico_play, 'Lire', '', None, False),
                'audio': (charte.ico_play, 'Jouer', 'STOP' if playing else '',
                          charte.GREEN if playing else None, playing),
                'py': (charte.ico_terminal, 'Exec', '', None, False),
                'zip': (charte.ico_copy, 'Nb', '', None, False),
                'autre': (charte.ico_folder, 'Parent', '', None, False),
            }[fk]
            caps = [
                (charte.ico_copy, 'Chemin', '', None, False),
                (charte.ico_copy, 'Unix', '', None, False),
                act3,
                (charte.ico_code, 'VS Code', '', None, False),
                (charte.ico_terminal, 'Term.', '', None, False),
                (charte.ico_clock, 'MAJ', '', None, False),
            ]
        else:
            caps = [
                (charte.ico_copy, 'Chemin',
                 'fichier' if sel else str(len(sel)) if sel else '',
                 None, False),
                (charte.ico_terminal, 'Terminal', '', None, False),
                (charte.ico_copy, 'Unix', '', None, False),
                (charte.ico_folder, 'Parent', '', None, False),
                (charte.ico_code, 'VS Code', '', None, bool(sel)),
                (charte.ico_clock, 'MAJ', '', None, False),
            ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'EXPL', charte.ORANGE)
        d.rounded_rectangle((106, 12, 146, 52), radius=8, fill=(60, 44, 16),
                            outline=charte.ORANGE)
        charte.ico_folder(d, 126, 32, 13, charte.ORANGE)
        nm = name
        while nm and d.textlength(nm, font=F_S) > 155:
            nm = nm[:-1]
        d.text((152, 24), nm, font=F_S, fill=charte.FG, anchor='lm')
        parent = os.path.dirname(path.rstrip('\\/')) if is_dir else ''
        d.text((152, 45), parent[:28], font=F_T, fill=charte.DIM, anchor='lm')
        d.line((106, 62, 313, 62), fill=charte.BORDER)
        d.text((106, 76), 'CHEMIN', font=F_T, fill=charte.DIM, anchor='lm')
        import textwrap
        yy = 94
        for ln in textwrap.wrap(path, 38)[:3]:
            d.text((106, yy), ln, font=F_XS, fill=(120, 160, 230),
                   anchor='lm')
            yy += 17
        d.line((106, yy - 4, 313, yy - 4), fill=charte.BORDER)
        if single:
            # zone fichier : nom + meta + miniature/infos selon le type
            d.text((106, yy + 8), 'SELECTION', font=F_T, fill=charte.ORANGE,
                   anchor='lm')
            import textwrap
            s0 = os.path.basename(single)
            y2 = yy + 26
            for ln in textwrap.wrap(s0, fk == 'image' and 20 or 34)[:2]:
                d.text((106, y2), ln, font=F_XS, fill=charte.FG, anchor='lm')
                y2 += 17
            try:
                st = os.stat(single)
                info = '{}  ·  {}'.format(self._fsize(st.st_size),
                                          self._mtime_h(single))
            except OSError:
                info = '?'
            extra = ''
            if fk == 'py':
                try:
                    with open(single, encoding='utf-8',
                              errors='replace') as fh:
                        extra = '  ·  {} lignes'.format(
                            sum(1 for _ in fh.readlines()[:2000]))
                except OSError:
                    pass
            if fk == 'zip':
                try:
                    import zipfile
                    with zipfile.ZipFile(single) as z:
                        extra = '  ·  {} fichiers'.format(len(z.infolist()))
                except Exception:
                    pass
            d.text((106, y2 + 2), (info + extra)[:40], font=F_T,
                   fill=charte.DIM, anchor='lm')
            if fk == 'image':
                try:
                    from PIL import Image as PILImage
                    im = PILImage.open(single).convert('RGB')
                    im.thumbnail((74, 56))
                    img.paste(im, (236, yy + 14), charte.rounded(im, 6))
                except Exception:
                    pass
            elif fk in ('video', 'audio'):
                charte.ico_play(d, 296, yy + 40, 8, charte.GREEN)
            elif fk == 'py':
                charte.ico_code(d, 296, yy + 40, 8, charte.BLUE)
        elif sel:
            d.text((106, yy + 8), 'SELECTION ({})'.format(len(sel)),
                   font=F_T, fill=charte.ORANGE, anchor='lm')
            import textwrap
            s0 = os.path.basename(sel[0])
            for ln in textwrap.wrap(s0, 34)[:2]:
                d.text((106, yy + 26), ln, font=F_XS, fill=charte.FG,
                       anchor='lm')
                yy += 17
            if len(sel) > 1:
                d.text((106, yy + 26), '+ {} autres'.format(len(sel) - 1),
                       font=F_T, fill=charte.DIM, anchor='lm')
        else:
            # stats : nb elements + espace libre du disque
            n_items = -1
            if is_dir:
                try:
                    n_items = sum(1 for _ in os.scandir(path))
                except OSError:
                    n_items = -1
            free = ''
            try:
                import shutil
                du = shutil.disk_usage(path)
                free = '{:.0f} Go libres'.format(du.free / 2**30)
            except Exception:
                pass
            x = 106
            if n_items >= 0:
                x += charte.pill(d, x, yy + 14, charte.ico_window,
                                 str(n_items), charte.BLUE) + 8
            if free:
                charte.pill(d, x, yy + 14, charte.ico_check, free,
                            charte.GREEN)

    def _rg_code(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        pl = self.pl or {}
        fname, ws = pl.get('file', '?'), pl.get('ws', '?')
        caps = [
            (charte.ico_copy, 'Fichier', '', None, False),
            (charte.ico_copy, 'Workspace', '', None, False),
            (charte.ico_home, 'Google', '', None, False),
            (charte.ico_pin, 'Devant', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'CODE', charte.BLUE)
        d.rounded_rectangle((106, 12, 146, 52), radius=8, fill=(16, 30, 52),
                            outline=charte.BLUE)
        charte.ico_code(d, 126, 32, 12, charte.BLUE)
        fn = fname
        while fn and d.textlength(fn, font=F_S) > 155:
            fn = fn[:-1]
        d.text((152, 24), fn, font=F_S, fill=charte.FG, anchor='lm')
        d.text((152, 45), ws[:28], font=F_T, fill=charte.DIM, anchor='lm')
        d.line((106, 62, 313, 62), fill=charte.BORDER)
        ext = os.path.splitext(fname)[1].lstrip('.').lower() or '?'
        x = 106
        x += charte.pill(d, x, 80, charte.ico_window, ext, charte.PURPLE) + 8
        charte.pill(d, x, 80, charte.ico_clock, self._duree(), charte.DIM)
        d.text((106, 110), 'EDITEUR', font=F_T, fill=charte.DIM, anchor='lm')
        d.text((106, 128), 'Visual Studio Code', font=F_XS, fill=charte.FG,
               anchor='lm')
        d.text((106, 150), 'raccourcis clavier locaux ; molette gauche =',
               font=F_T, fill=(90, 95, 110), anchor='lm')
        d.text((106, 164), 'changer de page FIP', font=F_T,
               fill=(90, 95, 110), anchor='lm')

    def _rg_terminal(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        cwd = (self.pl or {}).get('cwd')
        caps = [
            (charte.ico_copy, 'Chemin', '' if cwd else 'n/a', None, False),
            (charte.ico_folder, 'Ici', '' if cwd else 'n/a', None, False),
            (charte.ico_copy, 'Unix', '' if cwd else 'n/a', None, False),
            (charte.ico_home, 'Google', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'TERM', charte.GREEN)
        d.rounded_rectangle((106, 12, 146, 52), radius=8, fill=(14, 34, 22),
                            outline=charte.GREEN)
        charte.ico_terminal(d, 126, 32, 12, charte.GREEN)
        d.text((152, 24), 'Terminal', font=F_S, fill=charte.FG, anchor='lm')
        d.text((152, 45), self.wtitle[:28], font=F_T, fill=charte.DIM,
               anchor='lm')
        d.line((106, 62, 313, 62), fill=charte.BORDER)
        d.text((106, 76), 'REPERTOIRE', font=F_T, fill=charte.DIM,
               anchor='lm')
        if cwd:
            import textwrap
            yy = 94
            for ln in textwrap.wrap(cwd, 38)[:3]:
                d.text((106, yy), ln, font=F_XS, fill=(120, 160, 230),
                       anchor='lm')
                yy += 17
        else:
            d.text((106, 94), '(indetermine — le titre ne montre pas',
                   font=F_XS, fill=charte.DIM, anchor='lm')
            d.text((106, 111), 'pas de chemin)', font=F_XS, fill=charte.DIM,
                   anchor='lm')

    def _rg_vlc(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        name = (self.pl or {}).get('name', '?')
        caps = [
            (charte.ico_play, 'Pause', '', None, False),
            (charte.ico_x, 'Stop', '', None, False),
            (charte.ico_copy, 'Titre', '', None, False),
            (charte.ico_min, 'Muet', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'VLC', charte.ORANGE)
        av = charte.letter_avatar('vlc', 40, 9)
        img.paste(av, (106, 10), charte.rounded(av, 9))
        d.text((154, 24), 'VLC', font=F_S, fill=charte.FG, anchor='lm')
        d.text((154, 45), 'lecteur multimedia', font=F_T, fill=charte.DIM,
               anchor='lm')
        d.line((106, 62, 313, 62), fill=charte.BORDER)
        d.text((106, 76), 'EN COURS', font=F_T, fill=charte.DIM, anchor='lm')
        import textwrap
        yy = 96
        for ln in textwrap.wrap(name, 34)[:4]:
            d.text((106, yy), ln, font=F_XS, fill=charte.FG, anchor='lm')
            yy += 17
        d.text((106, yy + 10), 'raccourcis envoyes a la fenetre VLC',
               font=F_T, fill=(90, 95, 110), anchor='lm')

    def _rg_gmail(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        tab = self.tab or {}
        m = re.search(r'\(([\d\s\u202f.]+)\)', tab.get('title') or '')
        unread = 0
        if m:
            try:
                unread = int(m.group(1).replace(' ', '')
                             .replace('\u202f', '').replace('.', ''))
            except ValueError:
                unread = 0
        mail_addr = ''
        m2 = re.search(r'-\s*([\w.+-]+@[\w.-]+)', tab.get('title') or '')
        if m2:
            mail_addr = m2.group(1)
        caps = [
            (charte.ico_mail, 'Nouveau', '', None, False),
            (charte.ico_copy, 'Adresse', '' if mail_addr else 'n/a',
             None, False),
            (charte.ico_home, 'Google', '', None, False),
            (charte.ico_link, 'Copier URL', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'MAIL', charte.RED)
        d.rounded_rectangle((106, 12, 146, 52), radius=8, fill=(46, 16, 18),
                            outline=charte.RED)
        charte.ico_mail(d, 126, 32, 12, charte.RED)
        d.text((152, 24), 'Gmail', font=F_S, fill=charte.FG, anchor='lm')
        d.text((152, 45), (mail_addr or 'boite mail')[:28], font=F_T,
               fill=charte.DIM, anchor='lm')
        # badge messages non lus (titre de l'onglet)
        y = 78
        if unread:
            d.rounded_rectangle((106, y, 313, y + 46), radius=10,
                                fill=(52, 18, 20), outline=charte.RED)
            d.text((126, y + 23), str(unread), font=fipui.font(26),
                   fill=(255, 120, 110), anchor='lm')
            d.text((126 + 74, y + 23), 'messages non lus', font=F_XS,
                   fill=charte.FG, anchor='lm')
        else:
            d.rounded_rectangle((106, y, 313, y + 46), radius=10,
                                fill=(16, 40, 24), outline=charte.GREEN)
            d.text((210, y + 23), 'boite a jour', font=F_XS,
                   fill=charte.GREEN, anchor='mm')
        charte.ico_clock(d, 111, y + 68, 4, charte.DIM)
        pos, tot = self._tab_pos()
        meta = self._duree()
        if pos:
            meta += '   onglet {}/{}'.format(pos, tot)
        d.text((120, y + 68), meta, font=F_T, fill=charte.DIM, anchor='lm')
        d.text((106, y + 92), 'S1 = nouveau message', font=F_T,
               fill=(90, 95, 110), anchor='lm')

    def _rg_ghpage(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        pl = self.pl or {}
        is_pr = pl.get('kind') == 'pull'
        key = (pl.get('repo'), pl.get('kind'), pl.get('n'))
        c = self.ghd.get(key) or {}
        data = c.get('data')
        caps = [
            (charte.ico_link, 'Lien', '', None, False),
            (charte.ico_copy, 'Titre', '' if data else 'n/a', None, False),
            (charte.ico_home, 'Google', '', None, False),
            (charte.ico_copy, 'Fichiers' if is_pr else 'Corps', '',
             None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'PR' if is_pr else 'ISSUE',
                   charte.PURPLE if is_pr else charte.GREEN)
        d.text((106, 26), '#{}'.format(pl.get('n', '?')), font=fipui.font(26),
               fill=charte.FG, anchor='lm')
        d.text((160, 24), (pl.get('repo') or '?')[:24], font=F_XS,
               fill=charte.DIM, anchor='lm')
        d.text((160, 42), 'github.com', font=F_T, fill=(90, 95, 110),
               anchor='lm')
        d.line((106, 62, 313, 62), fill=charte.BORDER)
        if not c:
            d.text((210, 100), 'chargement gh...', font=F_XS,
                   fill=charte.DIM, anchor='mm')
            return
        if data is None:
            d.text((210, 90), 'gh:', font=F_XS, fill=charte.DIM, anchor='mm')
            d.text((210, 108), c.get('err', '?')[:34], font=F_XS,
                   fill=charte.RED, anchor='mm')
            return
        # etat
        state = (data.get('state') or '?').upper()
        scol = charte.GREEN if state == 'OPEN' else (
            charte.PURPLE if state == 'MERGED' else charte.RED)
        x = 106
        x += charte.pill(d, x, 80, charte.ico_check, state, scol) + 8
        who = (data.get('author') or {}).get('login', '?')
        x += charte.pill(d, x, 80, charte.ico_star, who[:14], charte.BLUE) + 8
        charte.pill(d, x, 80, charte.ico_issue,
                    '{} comm.'.format(len(data.get('comments') or [])),
                    charte.DIM)
        d.text((106, 104), 'TITRE', font=F_T, fill=charte.DIM, anchor='lm')
        import textwrap
        yy = 122
        for ln in textwrap.wrap(data.get('title') or '?', 34)[:3]:
            d.text((106, yy), ln, font=F_XS, fill=charte.FG, anchor='lm')
            yy += 17
        if is_pr:
            add = data.get('additions') or 0
            dele = data.get('deletions') or 0
            d.text((106, yy + 8), '+{}'.format(add), font=F_XS,
                   fill=charte.GREEN, anchor='lm')
            d.text((170, yy + 8), '-{}'.format(dele), font=F_XS,
                   fill=charte.RED, anchor='lm')
            d.text((230, yy + 8), '{} reviews'.format(
                len(data.get('reviews') or [])), font=F_T,
                   fill=charte.DIM, anchor='lm')
        d.text((106, yy + 28), 'cree ' + charte.ago(data.get('createdAt')),
               font=F_T, fill=(90, 95, 110), anchor='lm')

    def _rg_hf(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        model = self.pl or '?'
        c = self.hfd.get(model) or {}
        data = c.get('data')
        caps = [
            (charte.ico_copy, 'ID', '', None, False),
            (charte.ico_link, 'Page', '', None, False),
            (charte.ico_terminal, 'Cmd', '', None, False),
            (charte.ico_home, 'Google', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'HF', (240, 200, 60))
        d.rounded_rectangle((106, 12, 146, 52), radius=8, fill=(48, 40, 12),
                            outline=(240, 200, 60))
        charte.ico_star(d, 126, 32, 12, (240, 200, 60))
        import textwrap
        lines = textwrap.wrap(model, 26)[:2] or [model[:26]]
        d.text((152, 20), lines[0], font=F_S, fill=charte.FG, anchor='lm')
        if len(lines) > 1:
            d.text((152, 40), lines[1], font=F_XS, fill=charte.DIM,
                   anchor='lm')
        else:
            d.text((152, 42), 'huggingface.co', font=F_T,
                   fill=(90, 95, 110), anchor='lm')
        d.line((106, 62, 313, 62), fill=charte.BORDER)
        if not c:
            d.text((210, 100), 'chargement HF...', font=F_XS,
                   fill=charte.DIM, anchor='mm')
            return
        if data is None:
            d.text((210, 100), c.get('err', '?')[:34], font=F_XS,
                   fill=charte.RED, anchor='mm')
            return
        x = 106
        x += charte.pill(d, x, 80, charte.ico_star,
                         str(data.get('likes', 0)), charte.ORANGE) + 8
        dl = data.get('downloads') or 0
        x += charte.pill(d, x, 80, charte.ico_branch,
                         '{:.0f}k'.format(dl / 1000) if dl >= 1000
                         else str(dl), charte.BLUE) + 8
        if data.get('pipeline_tag'):
            charte.pill(d, x, 80, charte.ico_window,
                        data['pipeline_tag'][:12], charte.PURPLE)
        d.text((106, 104), 'MODELE', font=F_T, fill=charte.DIM, anchor='lm')
        rows = []
        if data.get('library_name'):
            rows.append('librairie ' + data['library_name'])
        if data.get('lastModified'):
            rows.append('maj ' + str(data['lastModified'])[:10])
        tg = [t for t in (data.get('tags') or [])
              if ':' not in t and len(t) < 14][:5]
        if tg:
            rows.append(' '.join(tg))
        yy = 122
        for r in rows[:5]:
            d.text((106, yy), r[:36], font=F_XS, fill=charte.FG, anchor='lm')
            yy += 17
        d.text((106, yy + 8), 'S3 = copier huggingface-cli download',
               font=F_T, fill=(90, 95, 110), anchor='lm')

    def _rg_gsearch(self, img, d):
        """Recherche Google : requete + RESULTAT DE CALCUL local + outils."""
        from urllib.parse import parse_qs as _pqs
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        u = urlsplit(self.pl or '')
        q = (_pqs(u.query).get('q') or [''])[0]
        tbm = (_pqs(u.query).get('tbm') or [''])[0]
        res = self._calc(q)
        caps = [
            (charte.ico_copy, 'Requete', '', None, False),
            (charte.ico_search, 'Lucky', '', None, False),
            (charte.ico_window, 'Images', '', None, tbm == 'isch'),
            (charte.ico_home, 'Actus', '', None, tbm == 'nws'),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'SEARCH', charte.BLUE)
        # barre de recherche visuelle
        d.rounded_rectangle((106, 14, 313, 58), radius=12, fill=charte.CARD,
                            outline=charte.BORDER, width=2)
        charte.ico_search(d, 124, 36, 8, charte.DIM)
        import textwrap
        qq = textwrap.wrap(q or '(vide)', 24)[:2] or [' ']
        d.text((140, 28), qq[0], font=F_XS, fill=charte.FG, anchor='lm')
        if len(qq) > 1:
            d.text((140, 45), qq[1], font=F_XS, fill=charte.DIM, anchor='lm')
        # resultat de calcul
        if res is not None:
            d.rounded_rectangle((106, 68, 313, 118), radius=10,
                                fill=(14, 40, 24), outline=charte.GREEN)
            d.text((106, 93), '=', font=fipui.font(26), fill=charte.GREEN,
                   anchor='lm')
            rs = ('{:g}'.format(res))
            while rs and d.textlength(rs, font=fipui.font(26)) > 170:
                rs = rs[:-1]
            d.text((140, 93), rs, font=fipui.font(26), fill=(150, 255, 180),
                   anchor='lm')
        else:
            d.text((210, 93), 'calcul ? tapez ex. 3*1.25', font=F_T,
                   fill=(90, 95, 110), anchor='mm')
        charte.ico_clock(d, 111, 134, 4, charte.DIM)
        pos, tot = self._tab_pos()
        meta = self._duree() + (
            '   onglet {}/{}'.format(pos, tot) if pos else '')
        d.text((120, 134), meta, font=F_T, fill=charte.DIM, anchor='lm')
        d.text((106, 156), 'OUTILS', font=F_T, fill=charte.DIM, anchor='lm')
        for i, ln in enumerate([
                'S2 Lucky = premier resultat',
                'S3 recherche d images',
                'S4 recherche d actualites']):
            d.text((106, 174 + i * 17), ln, font=F_XS, fill=charte.FG,
                   anchor='lm')

    def _rg_chat(self, img, d):
        """Conversations IA (z.ai, ChatGPT, Claude...) : titre + raccourcis."""
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        tab = self.tab or {}
        host = urlsplit(self.pl or '').netloc.replace('www.', '')
        caps = [
            (charte.ico_copy, 'Titre', '', None, False),
            (charte.ico_link, 'URL', '', None, False),
            (charte.ico_home, 'Accueil', '', None, False),
            (charte.ico_search, 'Google', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'AI', charte.PURPLE)
        d.rounded_rectangle((106, 12, 146, 52), radius=8, fill=(38, 20, 56),
                            outline=charte.PURPLE)
        charte.ico_star(d, 126, 32, 11, charte.PURPLE)
        d.text((152, 24), host[:20], font=F_S, fill=charte.FG, anchor='lm')
        d.text((152, 45), 'assistant IA', font=F_T, fill=charte.DIM,
               anchor='lm')
        d.line((106, 62, 313, 62), fill=charte.BORDER)
        d.text((106, 76), 'CONVERSATION', font=F_T, fill=charte.DIM,
               anchor='lm')
        import textwrap
        title = (tab.get('title') or host).replace(' - ' + host, '')
        yy = 96
        for ln in textwrap.wrap(title, 32)[:4]:
            d.text((106, yy), ln, font=F_XS, fill=charte.FG, anchor='lm')
            yy += 17
        d.line((106, yy - 6, 313, yy - 6), fill=charte.BORDER)
        charte.ico_clock(d, 111, yy + 8, 4, charte.DIM)
        pos, tot = self._tab_pos()
        meta = self._duree() + (
            '   onglet {}/{}'.format(pos, tot) if pos else '')
        d.text((120, yy + 8), meta, font=F_T, fill=charte.DIM, anchor='lm')
        d.text((106, yy + 26), 'S3 Accueil = nouvelle conversation',
               font=F_T, fill=(90, 95, 110), anchor='lm')

    def _rg_default(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        # ---- colonne GAUCHE (~30%)
        armed = self._armed and time.time() - self._armed[1] < 3.0 \
            and self._armed[0]
        caps = [
            (charte.ico_copy, 'Titre', '', None, False),
            (charte.ico_pin, 'Devant', 'ON' if self.topmost else '',
             charte.BLUE if self.topmost else None, self.topmost),
            (charte.ico_min, 'Minimiser', '', None, False),
            (charte.ico_x, 'Tuer', '2x', charte.RED if armed == 'kill'
             else None, armed == 'kill'),
            (charte.ico_play, 'Relancer', '2x',
             charte.ORANGE if armed == 'restart' else None,
             armed == 'restart'),
            (charte.ico_copy, 'Chemin', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        # ---- colonne DROITE (~70%)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'APP', charte.TEAL)
        av = charte.letter_avatar(self.proc or '?', 44, 10)
        img.paste(av, (106, 12), charte.rounded(av, 10))
        d.text((158, 22), (self.proc or '?')[:22], font=F_S, fill=charte.FG,
               anchor='lm')
        d.text((158, 44), 'PID {}'.format(self.pid or '?'), font=F_XS,
               fill=charte.DIM, anchor='lm')
        if self.rect:
            charte.ico_window(d, 163, 62, 4, charte.DIM)
            d.text((172, 62), '{}x{}'.format(*self.rect), font=F_XS,
                   fill=charte.DIM, anchor='lm')
        d.text((106, 92), 'TITRE', font=F_T, fill=charte.DIM, anchor='lm')
        import textwrap
        lines = textwrap.wrap(self.wtitle or '(sans titre)', width=32) or \
            ['(sans titre)']
        y = 110
        for ln in lines[self.scroll:self.scroll + 7]:
            d.text((106, y), ln, font=F_XS, fill=charte.FG, anchor='lm')
            y += 17
        if len(lines) > 7:
            d.text((311, 92), '{}/{}'.format(
                min(self.scroll + 7, len(lines)), len(lines)), font=F_T,
                fill=(90, 95, 110), anchor='rm')
            d.text((210, 228), 'molette = defiler', font=F_T,
                   fill=(90, 95, 110), anchor='mm')
