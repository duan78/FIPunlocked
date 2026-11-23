"""
taskmgr.py — gestionnaire de taches express : les 6 processus les plus
gourmands (CPU / RAM / disque) alignes sur S1-S6, barre de charge, et kill
en deux appuis (confirmation).

  molette droite = changer le tri (CPU -> RAM -> DISQUE)
  S<n> 1er appui = armer le kill (rangee encadree, 3 s pour confirmer)
  S<n> 2e appui  = tuer CE processus (la confirmation suit le processus,
                   meme si la liste est retriee entre les deux appuis)
Processus systeme critiques et python lui-meme : proteges (non tuables).

PERFORMANCE : les processus sont lus avec UN SEUL appel noyau par passe
(NtQuerySystemInformation). Les appels psutil par processus (cpu_times,
memory_info, io_counters) prennent chacun un instantané de TOUS les
processus : ~10 ms sur un gros poste, soit plusieurs secondes par passe.
psutil reste en repli si l'instantané n'est pas exploitable.
La mesure ne tourne QUE pendant que la page est affichée (fipui poll).
"""
import ctypes
import ctypes.wintypes as wt
import os
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
    'python.exe', 'pythonw.exe', 'conhost.exe',
}
MY_PID = os.getpid()


# ---------------------- instantane de tous les processus ----------------------

class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', wt.USHORT), ('MaximumLength', wt.USHORT),
                ('Buffer', ctypes.c_void_p)]


class _SPI(ctypes.Structure):
    """SYSTEM_PROCESS_INFORMATION (x64, Vista+)."""
    _fields_ = [
        ('NextEntryOffset', wt.ULONG), ('NumberOfThreads', wt.ULONG),
        ('WorkingSetPrivateSize', ctypes.c_longlong), ('HardFaultCount', wt.ULONG),
        ('NumberOfThreadsHighWatermark', wt.ULONG), ('CycleTime', ctypes.c_ulonglong),
        ('CreateTime', ctypes.c_longlong), ('UserTime', ctypes.c_longlong),
        ('KernelTime', ctypes.c_longlong), ('ImageName', _UNICODE_STRING),
        ('BasePriority', wt.LONG), ('UniqueProcessId', ctypes.c_void_p),
        ('InheritedFromUniqueProcessId', ctypes.c_void_p),
        ('HandleCount', wt.ULONG), ('SessionId', wt.ULONG),
        ('UniqueProcessKey', ctypes.c_void_p),
        ('PeakVirtualSize', ctypes.c_size_t), ('VirtualSize', ctypes.c_size_t),
        ('PageFaultCount', wt.ULONG),
        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
        ('PrivatePageCount', ctypes.c_size_t),
        ('ReadOperationCount', ctypes.c_ulonglong),
        ('WriteOperationCount', ctypes.c_ulonglong),
        ('OtherOperationCount', ctypes.c_ulonglong),
        ('ReadTransferCount', ctypes.c_ulonglong),
        ('WriteTransferCount', ctypes.c_ulonglong),
        ('OtherTransferCount', ctypes.c_ulonglong),
    ]


# disposition attendue sur Windows x64 ; sinon repli psutil
_SNAP_OK = (ctypes.sizeof(ctypes.c_void_p) == 8 and ctypes.sizeof(_SPI) == 256
            and _SPI.UniqueProcessId.offset == 80 and _SPI.WorkingSetSize.offset == 144
            and _SPI.ReadTransferCount.offset == 232 and _SPI.WriteTransferCount.offset == 240)
_ntdll = ctypes.WinDLL('ntdll')
_ntdll.NtQuerySystemInformation.argtypes = [wt.ULONG, ctypes.c_void_p, wt.ULONG,
                                             ctypes.POINTER(wt.ULONG)]
_ntdll.NtQuerySystemInformation.restype = wt.ULONG
_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


def _system_snapshot():
    """UN appel noyau -> {pid: {'name', 'cpu' (temps en 100 ns), 'rss' (octets),
    'io' (octets lus+ecrits)}} ; None si l'instantane est inexploitable."""
    if not _SNAP_OK:
        return None
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        got = wt.ULONG()
        st = _ntdll.NtQuerySystemInformation(
            _SYSTEM_PROCESS_INFORMATION, buf, size, ctypes.byref(got)) & 0xFFFFFFFF
        if st != _STATUS_INFO_LENGTH_MISMATCH:
            break
        size *= 2
        if size > 1 << 27:
            return None
    if st != 0:
        return None
    out = {}
    base, off = ctypes.addressof(buf), 0
    while True:
        e = _SPI.from_address(base + off)
        pid = e.UniqueProcessId or 0
        if e.ImageName.Buffer:
            name = ctypes.wstring_at(e.ImageName.Buffer, e.ImageName.Length // 2)
        else:
            name = 'System Idle Process' if pid == 0 else 'System'
        out[pid] = {'name': name, 'cpu': e.UserTime + e.KernelTime,
                    'rss': e.WorkingSetSize,
                    'io': e.ReadTransferCount + e.WriteTransferCount}
        if not e.NextEntryOffset:
            break
        off += e.NextEntryOffset
    return out if len(out) > 8 else None


def _psutil_snapshot():
    """Repli (lent) : memes informations via psutil, un instantane par processus."""
    out = {}
    for p in psutil.process_iter(['pid', 'name']):
        try:
            with p.oneshot():
                t = p.cpu_times()
                m = p.memory_info()
                io = p.io_counters()
            out[p.info['pid']] = {'name': p.info['name'] or '?',
                                  'cpu': int((t.user + t.system) * 1e7),
                                  'rss': m.rss,
                                  'io': io.read_bytes + io.write_bytes}
        except psutil.Error:
            continue
    return out


def _snapshot():
    return _system_snapshot() or _psutil_snapshot()


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
        self._armed = None               # (pid, t0)
        self._prev = None                # instantané précédent {pid: {...}}
        self._t = 0.0                    # date (monotone) de cet instantané
        self.poll(self._measure, POLL_S) # page affichée seulement

    # ---------- mesure (une passe) ----------

    def _measure(self):
        snap = _snapshot()
        now = time.monotonic()
        prev, t0 = self._prev, self._t
        if prev is None:                 # 1re passe (entrée de page) : delta court
            prev, t0 = snap, now
            time.sleep(0.3)
            snap = _snapshot()
            now = time.monotonic()
        ncpu = psutil.cpu_count() or 1
        total = psutil.virtual_memory().total or 1
        dt = now - t0
        rows = []
        for pid, s in snap.items():
            p = prev.get(pid)
            if not p or s['name'].lower() == 'system idle process' or pid < 500:
                continue                   # bruit systeme ininteressant
            rows.append({
                'pid': pid, 'name': s['name'].lower(),
                'cpu': (max(0, s['cpu'] - p['cpu']) / 1e7) / dt * 100 / ncpu if dt > 0 else 0.0,
                'mem': s['rss'] / total * 100,
                'io': max(0, s['io'] - p['io']) / 1024 / dt if dt > 0 else 0.0,
            })
        self._prev, self._t = snap, now
        key = (lambda r: (-r['cpu'], -r['mem']),
               lambda r: (-r['mem'], -r['cpu']),
               lambda r: (-r['io'], -r['mem']))[self.sort]
        rows.sort(key=key)
        self.procs = rows[:6]
        self.cpu_tot = psutil.cpu_percent(interval=None)
        self.mem_tot = psutil.virtual_memory().percent

    # ---------- interactions ----------

    def _protected(self, r):
        return r['pid'] < 500 or r['pid'] == MY_PID or r['name'] in PROTECTED

    def _armed_pid(self):
        if self._armed and time.time() - self._armed[1] < 3.0:
            return self._armed[0]
        self._armed = None                 # delai depasse
        return None

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
        if self._protected(r):             # jamais tuable : pas d'armement
            self._armed = None
            self.alert('protege: ' + r['name'])
            return
        # confirmation : 2e appui sur le MEME PROCESSUS dans les 3 s
        if self._armed_pid() == r['pid']:
            self._armed = None
            try:
                psutil.Process(r['pid']).kill()
                self.alert(r['name'] + ' arrete')
            except Exception as e:
                self.alert('echec: ' + str(e)[:24])
        else:
            self._armed = (r['pid'], time.time())
            self.alert('re-appuyez pour tuer')

    def http_get(self):
        return {'sort': SORTS[self.sort],
                'procs': [{k: r[k] for k in ('pid', 'name', 'cpu', 'mem', 'io')}
                          for r in self.procs]}

    # ---------- rendu ----------

    def render(self, d):
        F_S = fipui.font(18)
        F_XS = fipui.font(13)
        armed = self._armed_pid()
        d.rectangle((0, 0, 319, 23), fill=(28, 33, 50))
        d.text((8, 12), 'TACHES — tri ' + SORTS[self.sort], font=F_S,
               fill=fipui.ACCENT, anchor='lm')
        d.text((312, 12), 'CPU {:.0f}%  RAM {:.0f}%'.format(
            self.cpu_tot, self.mem_tot), font=F_XS, fill=fipui.DIM, anchor='rm')
        for i, r in enumerate(self.procs):
            y = 26 + i * 36
            yc = y + 16
            val, ratio = {
                'CPU': ('{:5.1f}%'.format(r['cpu']), r['cpu'] / 100),
                'RAM': ('{:5.1f}%'.format(r['mem']), r['mem'] / 100),
                'DISQUE': (_fmt_io(r['io']), min(1.0, r['io'] / 51200)),
            }[SORTS[self.sort]]
            hot = armed == r['pid']        # rangee armee : encadree
            if hot:
                d.rectangle((2, y, 317, y + 33), outline=fipui.RED, width=2)
            d.text((10, yc - 7), '{}  [{}]'.format(r['name'][:20], r['pid']),
                   font=F_XS, fill=fipui.FG, anchor='lm')
            d.text((10, yc + 9), val, font=F_XS,
                   fill=fipui.ACCENT if hot else fipui.DIM, anchor='lm')
            fipui.draw_bar(d, 120, yc + 3, 270, yc + 15, ratio,
                           fipui.RED if hot else fipui.BLUE)
            d.text((314, yc), f'S{i + 1}', font=F_XS, fill=(90, 95, 110),
                   anchor='rm')
