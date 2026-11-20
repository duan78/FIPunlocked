"""
githubci.py — dashboard CI GitHub Actions (via gh CLI).

Config apps/github.json (creee au premier lancement avec vos repos) :
  {"repos": ["moi/repo1", "moi/repo2"], "poll_s": 60}

Ecran : une rangee par repo (alignee sur S1-S6), points de statut des
derniers runs (vert = succes, rouge = echec, bleu = en cours, gris = autre).
Molette droite = defilement si plus de 6 repos.
Bouton S<n> = ouvrir le dernier run du repo correspondant dans le navigateur.
Alerte FIP + LED 8 quand un run passe en echec (une seule fois par run).
"""
import json
import os
import subprocess
import threading
import time

import fipui
from fipui import App

CREATE_NO_WINDOW = 0x08000000
CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'github.json')
RUN_FIELDS = 'databaseId,status,conclusion,workflowName,headBranch,url'
DOT_COLORS = {'success': fipui.GREEN, 'failure': fipui.RED,
              'cancelled': (120, 125, 140), 'startup_failure': fipui.RED}
STATUS_CHARS = {'success': 'OK', 'failure': 'ECHEC', 'in_progress': 'EN COURS',
                'queued': 'EN FILE'}


def _gh(args, timeout=20):
    out = subprocess.run(['gh'] + args, capture_output=True, timeout=timeout,
                         creationflags=CREATE_NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode('utf-8', 'replace').strip()[:120])
    return json.loads(out.stdout.decode('utf-8', 'replace'))


class GithubCiApp(App):
    name = 'githubci'
    title = 'CI GITHUB'
    show_footer = False

    def __init__(self, daemon):
        super().__init__(daemon)
        self.repos = []             # [{'name', 'runs': [...], 'error'}]
        self.view = 0
        self.status = 'initialisation...'
        self._seen_fail = {}        # repo -> {run_id: bool}
        self._primed = set()        # repos ayant eu un 1er poll complet
        self._stop = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------- config ----------

    def _load_cfg(self):
        if os.path.exists(CFG):
            try:
                with open(CFG, encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print('[githubci] config illisible:', e, flush=True)
        cfg = {'repos': [], 'poll_s': 60}
        try:                                    # auto-seed : vos repos sources
            data = _gh(['repo', 'list', '--limit', '6', '--source',
                        '--json', 'nameWithOwner'])
            cfg['repos'] = [r['nameWithOwner'] for r in data]
        except Exception as e:
            print('[githubci] gh repo list:', e, flush=True)
        try:
            with open(CFG, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2)
        except OSError:
            pass
        return cfg

    # ---------- boucle de poll ----------

    def _loop(self):
        cfg = self._load_cfg()
        names = cfg.get('repos') or []
        with self._lock:
            self.repos = [{'name': n, 'runs': [], 'error': None} for n in names]
        if not names:
            self.status = 'aucun repo (apps/github.json)'
            return
        period = max(20, int(cfg.get('poll_s', 60)))
        while not self._stop.is_set():
            for repo in list(self.repos):
                if self._stop.is_set():
                    break
                try:
                    runs = _gh(['run', 'list', '-R', repo['name'],
                                '--limit', '4', '--json', RUN_FIELDS])
                    self._check_alerts(repo['name'], runs)
                    with self._lock:
                        repo['runs'] = runs
                        repo['error'] = None
                    self.status = 'maj ' + time.strftime('%H:%M:%S')
                except Exception as e:
                    with self._lock:
                        repo['error'] = str(e)[:60]
            self._stop.wait(period)

    def _check_alerts(self, repo, runs):
        seen = self._seen_fail.setdefault(repo, {})
        primed = repo in self._primed
        for r in runs:
            rid = r['databaseId']
            if r.get('conclusion') == 'failure' and not seen.get(rid):
                if primed:                        # pas d'alerte au 1er poll
                    self.alert(f'{repo.split("/")[-1]}: ECHEC CI')
            seen[rid] = True
        self._primed.add(repo)
        # purge des ids disparus
        live = {r['databaseId'] for r in runs}
        for rid in list(seen):
            if rid not in live:
                del seen[rid]

    # ---------- interactions ----------

    def on_event(self, name, pressed):
        if not pressed:
            return
        n = len(self.repos)
        if name == 'right_cw' and n > 6:
            self.view = min(self.view + 1, n - 6)
        elif name == 'right_ccw' and n > 6:
            self.view = max(self.view - 1, 0)
        elif name in ('S1', 'S2', 'S3', 'S4', 'S5', 'S6'):
            idx = self.view + int(name[1]) - 1
            if idx >= n:
                return
            repo = self.repos[idx]
            run = repo['runs'][0] if repo['runs'] else None
            url = (run or {}).get('url') or \
                f"https://github.com/{repo['name']}/actions"
            import webbrowser
            webbrowser.open(url)
            self.alert('ouverture ' + repo['name'].split('/')[-1])

    def http_get(self):
        with self._lock:
            return {'status': self.status,
                    'repos': [{'name': r['name'], 'error': r['error'],
                               'last': r['runs'][0] if r['runs'] else None}
                              for r in self.repos]}

    # ---------- rendu ----------

    def render(self, d):
        F_S = fipui.font(18)
        F_XS = fipui.font(13)
        d.rectangle((0, 0, 319, 23), fill=(28, 33, 50))
        d.text((8, 12), 'CI GITHUB', font=F_S, fill=fipui.ACCENT, anchor='lm')
        d.text((312, 12), self.status[:20], font=F_XS, fill=fipui.DIM,
               anchor='rm')
        with self._lock:
            repos = list(self.repos)
        if not repos:
            d.text((160, 120), 'Aucun repo suivi', font=F_S, fill=fipui.DIM,
                   anchor='mm')
            d.text((160, 150), 'editez apps/github.json puis', font=F_XS,
                   fill=fipui.DIM, anchor='mm')
            d.text((160, 168), 'POST /apps/reload', font=F_XS, fill=fipui.DIM,
                   anchor='mm')
            return
        y = 28
        for i in range(self.view, min(self.view + 6, len(repos))):
            repo = repos[i]
            yc = y + 17
            if repo['error']:
                d.text((14, yc), repo['name'][:22], font=F_S, fill=fipui.RED,
                       anchor='lm')
                d.text((14, y + 30), repo['error'][:34], font=F_XS,
                       fill=(160, 100, 95), anchor='lm')
            elif repo['runs']:
                name = repo['name']
                if d.textlength(name, font=F_S) > 208:
                    while name and d.textlength(name + '...', font=F_S) > 208:
                        name = name[:-1]
                    name += '...'
                d.text((14, yc), name, font=F_S, fill=fipui.FG, anchor='lm')
                run = repo['runs'][0]
                wf = run.get('workflowName') or '?'
                if d.textlength(wf, font=F_XS) > 190:
                    while wf and d.textlength(wf + '...', font=F_XS) > 190:
                        wf = wf[:-1]
                    wf += '...'
                d.text((14, y + 30), wf, font=F_XS, fill=fipui.DIM,
                       anchor='lm')
                d.text((316, y + 30), (run.get('headBranch') or '')[:10],
                       font=F_XS, fill=(100, 140, 200), anchor='rm')
                # points des derniers runs (vert/rouge/bleu/gris)
                x = 250
                for r2 in repo['runs'][:3]:
                    c2 = fipui.BLUE if r2['status'] != 'completed' else \
                        DOT_COLORS.get(r2.get('conclusion'), (120, 125, 140))
                    d.ellipse((x, yc - 5, x + 10, yc + 5), fill=c2)
                    x += 18
            else:
                d.text((14, yc), repo['name'][:22], font=F_S,
                       fill=fipui.DIM, anchor='lm')
                d.text((14, y + 30), 'aucun run', font=F_XS,
                       fill=(90, 95, 110), anchor='lm')
            d.text((316, yc), f'S{i - self.view + 1}', font=F_XS,
                   fill=(90, 95, 110), anchor='rm')
            d.line((0, y + 35, 319, y + 35), fill=(32, 38, 56))
            y += 35
        if len(repos) > 6:
            d.rectangle((317, 28, 319, 238), fill=(30, 35, 50))
            th = 210 * 6 // len(repos)
            ty = 28 + (210 - th) * self.view // max(1, len(repos) - 6)
            d.rectangle((317, ty, 319, ty + th), fill=fipui.BLUE)
