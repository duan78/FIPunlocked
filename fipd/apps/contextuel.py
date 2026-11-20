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
        self.tab = None                # onglet actif (extension Chrome)
        self.kind = 'app'              # github | youtube | site | app
        self.pl = None                 # payload du contexte (repo/url)
        self.yt = {}                   # url youtube -> {'data', 'img'}
        self.fav = {}                  # host -> favicon PIL (ou None)
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
            changed = (repo, kind, pl, proc, title[:60]) != last
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
            if 'github.com' in host:
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
        return 'app', None, None

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
        if self.kind == 'youtube' and self.pl:
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
                'kind': self.kind, 'payload': (self.pl or '')[:120],
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

    def _rg_site(self, img, d):
        F_S = fipui.font(15)
        F_XS = fipui.font(13)
        F_T = fipui.font(11)
        tab = self.tab or {}
        host = urlsplit(self.pl or '').netloc
        caps = [
            (charte.ico_link, 'URL', '', None, False),
            (charte.ico_copy, 'Titre', '', None, False),
            (charte.ico_home, 'Google', '', None, False),
            (charte.ico_copy, 'Domaine', '', None, False),
            (charte.ico_min, 'Vol-', '', None, False),
            (charte.ico_min, 'Vol+', '', None, False),
        ]
        for i, (ic, lab, val, col, hot) in enumerate(caps):
            charte.keycap(d, 4 + i * 39, lab, value=val, icon=ic, col=col,
                          hot=hot, h=35, x0=4, x1=94, fsize=11, r=4)
        charte.panel(d, (98, 4, 317, 238))
        self._chip(d, 311, 10, 'WEB', charte.TEAL)
        fav = self.fav.get(host)
        if fav is not None:
            f2 = fav.resize((36, 36))
            img.paste(f2, (106, 12), f2)
        else:
            av = charte.letter_avatar(host, 36, 8)
            img.paste(av, (106, 12), charte.rounded(av, 8))
        h2 = host.replace('www.', '')[:24]
        d.text((152, 30), h2, font=F_S, fill=charte.FG, anchor='lm')
        import textwrap
        lines = textwrap.wrap(tab.get('title') or host, 32)[:3] or [' ']
        yy = 68
        d.text((106, yy), 'TITRE', font=F_T, fill=charte.DIM, anchor='lm')
        yy += 16
        for ln in lines:
            d.text((106, yy), ln, font=F_XS, fill=charte.FG, anchor='lm')
            yy += 17
        d.text((106, yy + 6), 'URL', font=F_T, fill=charte.DIM, anchor='lm')
        yy += 22
        for ln in textwrap.wrap(self.pl or '', 44)[:2]:
            d.text((106, yy), ln, font=F_T, fill=(110, 118, 129),
                   anchor='lm')
            yy += 13

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
