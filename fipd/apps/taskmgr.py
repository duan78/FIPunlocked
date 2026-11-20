"""
taskmgr.py — gestionnaire de taches express : les 6 processus les plus
gourmands (CPU / RAM / disque) alignes sur S1-S6, barre de charge, et kill
en deux appuis (confirmation).

  molette droite = changer le tri (CPU -> RAM -> DISQUE)
  S<n> 1er appui = armer le kill (rangee encadree, 3 s pour confirmer)
  S<n> 2e appui  = tuer le processus
Processus systeme critiques et python lui-meme : proteges (alerte).
"""
import os
import threading
import time

import fipui
import psutil
from fipui import App

POLL_S = 2.0
SORTS = ['CPU', 'RAM', 'DISQUE']
PROTECTED = {
    'system', 'idle', 'registry', 'smss.exe', 'csrss.exe', 'wininit.exe',
    'winlogon.exe', 'services.exe', 'lsass.exe', 'svchost.exe', 'dwm.exe',
    'explorer.exe', 'fontdrvhost.exe', 'sihost.exe', 'ctfmon.exe',
    'python.exe', 'pythonw.exe', 'conhost.exe', 'dwm.exe',
}
MY_PID = os.getpid()


def _fmt_io(kb):
    if kb > 1024 * 1024:
        return '{:.1f} Go/s'.format(kb / 1024 / 1024)
    if kb > 1024:
        return '{:.0f} Mo/s'.format(kb / 1024)
    return '{:.0f} Ko/s'.format(kb)


class TaskMgrApp(App):
    name = 'taskmgr'
    title = 'TACHES'
    show_footer = False

    def __init__(self, daemon):
        super().__init__(daemon)
        self.sort = 0                    # index dans SORTS
        self.procs = []                  # top6 : dicts {pid,name,cpu,mem,io}
        self.cpu_tot = 0.0
        self.mem_tot = 0.0
        self._armed = None               # (row, pid, name, t0)
        self._stop = threading.Event()
        self._prev_io = {}               # pid -> (bytes, t)
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------- boucle de mesure ----------

    def _loop(self):
        psutil.cpu_percent(interval=None)          # amorcage
        while not self._stop.is_set():
            t = time.time()
            rows = []
            for p in psutil.process_iter(['pid', 'name']):
                try:
                    pid = p.info['pid']
                    name = (p.info['name'] or '?').lower()
                    if name == 'system idle process' or pid < 500:
                        continue                   # bruit systeme ininteressant
                    cpu = p.cpu_percent(interval=None)   # delta depuis le 1er appel
                    mem = p.memory_percent()
                    io_kb = 0.0
                    try:
                        io = p.io_counters()
                        prev = self._prev_io.get(pid)
                        self._prev_io[pid] = (io.read_bytes + io.write_bytes, t)
                        if prev:
                            io_kb = max(0, (self._prev_io[pid][0] - prev[0])) / 1024 / (t - prev[1])
                    except Exception:
                        pass
                    rows.append({'pid': pid, 'name': name, 'cpu': cpu,
                                 'mem': mem, 'io': io_kb, 'proc': p})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self._prev_io = {pid: v for pid, v in self._prev_io.items()
                             if t - v[1] < 10}
            key = (lambda r: (-r['cpu'], -r['mem']),
                   lambda r: (-r['mem'], -r['cpu']),
                   lambda r: (-r['io'], -r['mem']))[self.sort]
            rows.sort(key=key)
            self.procs = rows[:6]
            self.cpu_tot = psutil.cpu_percent(interval=None)
            self.mem_tot = psutil.virtual_memory().percent
            self._stop.wait(POLL_S)

    # ---------- interactions ----------

    def _protected(self, r):
        return (r['pid'] < 500 or r['pid'] == MY_PID
                or r['name'] in PROTECTED)

    def on_event(self, name, pressed):
        if not pressed:
            return
        if name in ('right_cw', 'right_ccw'):
            self.sort = (self.sort + (1 if name == 'right_cw' else -1)) % len(SORTS)
            self._armed = None
            return
        if name not in ('S1', 'S2', 'S3', 'S4', 'S5', 'S6'):
            return
        row = int(name[1]) - 1
        if row >= len(self.procs):
            return
        r = self.procs[row]
        # confirmation : 2e appui sur la MEME rangee dans les 3 s
        if (self._armed and self._armed[0] == row
                and time.time() - self._armed[3] < 3.0):
            if self._protected(r):
                self.alert('protege: ' + r['name'])
            else:
                try:
                    r['proc'].kill()
                    self.alert(r['name'] + ' arrete')
                except Exception as e:
                    self.alert('echec: ' + str(e)[:24])
            self._armed = None
        else:
            self._armed = (row, r['pid'], r['name'], time.time())
            if self._protected(r):
                self.alert('protege: 2e appui = forcer')
            else:
                self.alert('re-appuyez pour tuer')

    def http_get(self):
        return {'sort': SORTS[self.sort],
                'procs': [{k: r[k] for k in ('pid', 'name', 'cpu', 'mem', 'io')}
                          for r in self.procs]}

    # ---------- rendu ----------

    def render(self, d):
        F_S = fipui.font(18)
        F_XS = fipui.font(13)
        if self._armed and time.time() - self._armed[3] >= 3.0:
            self._armed = None
        d.rectangle((0, 0, 319, 23), fill=(28, 33, 50))
        d.text((8, 12), 'TACHES — tri ' + SORTS[self.sort], font=F_S,
               fill=fipui.ACCENT, anchor='lm')
        d.text((312, 12), 'CPU {:.0f}%  RAM {:.0f}%'.format(
            self.cpu_tot, self.mem_tot), font=F_XS, fill=fipui.DIM, anchor='rm')
        armed_row = self._armed[0] if self._armed else None
        for i, r in enumerate(self.procs):
            y = 26 + i * 36
            yc = y + 16
            val, ratio = {
                'CPU': ('{:5.1f}%'.format(r['cpu']), r['cpu'] / 100),
                'RAM': ('{:5.1f}%'.format(r['mem']), r['mem'] / 100),
                'DISQUE': (_fmt_io(r['io']), min(1.0, r['io'] / 51200)),
            }[SORTS[self.sort]]
            if i == armed_row:                      # rangee armee : encadree
                d.rectangle((2, y, 317, y + 33), outline=fipui.RED, width=2)
            d.text((10, yc - 7), '{}  [{}]'.format(r['name'][:20], r['pid']),
                   font=F_XS, fill=fipui.FG, anchor='lm')
            d.text((10, yc + 9), val, font=F_XS,
                   fill=fipui.ACCENT if i == armed_row else fipui.DIM,
                   anchor='lm')
            fipui.draw_bar(d, 120, yc + 3, 270, yc + 15, ratio,
                           fipui.RED if i == armed_row else fipui.BLUE)
            d.text((314, yc), f'S{i + 1}', font=F_XS, fill=(90, 95, 110),
                   anchor='rm')
        d.text((160, 231), 'S = tuer (2 appuis) - molette = tri',
               font=F_XS, fill=(120, 120, 130), anchor='mm')
