"""
orchestrateur.py — Sequence complete de demarrage du FIP "a nous".

1. Nettoie les anciens processus
2. Lance SPAD via PowerShell (python-Popen le tuait !)
3. Attend l'initialisation du FIP par SPAD (log 'Turn device ON', patience 6 min :
   le dialogue Warnings de SPAD se ferme tout seul en ~2 min)
4. Tue SPAD
5. Lance fipd (FIPD_SKIP_INIT=1) : notre backend pur (ecran, interface, video)
   + lecteur HID boutons demarre immediatement
"""
import os
import subprocess
import sys
import time

SPAD = r'C:\Program Files\SPAD.neXt\SPAD.neXt.exe'
LOG = os.path.expandvars(r'%APPDATA%\SPAD.neXt\logs\spad.log')
HERE = os.path.dirname(os.path.abspath(__file__))


def ps(cmd):
    subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True)


def attendre_init(delai_max=360):
    """Attend une NOUVELLE ligne 'Turn device ON' dans le log SPAD."""
    try:
        pos = os.path.getsize(LOG)
    except OSError:
        pos = 0
    t0 = time.time()
    while time.time() - t0 < delai_max:
        time.sleep(3)
        try:
            if os.path.getsize(LOG) < pos:
                pos = 0
            with open(LOG, 'rb') as f:
                f.seek(pos)
                txt = f.read()
        except OSError:
            continue
        if b'MZBEAF15DB' in txt and (b'Turn device ON' in txt or b'Version fip' in txt):
            return True
    return False


def main():
    print('== FIP : demarrage complet ==', flush=True)
    ps("Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force")
    ps("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
       "Where-Object { $_.CommandLine -match 'fipd|run_fipd' } | "
       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    time.sleep(2)

    print('1/3 SPAD lance (initialisation du firmware, ~2-4 min)...', flush=True)
    ps(f"Start-Process '{SPAD}'")
    ok = attendre_init()
    print(f'   init FIP : {"OK" if ok else "ECHEC (powercycle USB puis relancez)"}', flush=True)
    if not ok:
        ps("Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force")
        sys.exit(1)
    time.sleep(6)
    ps("Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force")
    print('2/3 SPAD arrete — le firmware est actif.', flush=True)

    print('3/3 fipd demarre (interface + video + boutons).', flush=True)
    env = dict(os.environ, FIPD_SKIP_INIT='1')
    p = subprocess.Popen([sys.executable, os.path.join(HERE, 'fipd.py')],
                         cwd=HERE, env=env)
    time.sleep(8)
    print(f'\nDaemon actif (pid {p.pid}) : http://127.0.0.1:8199', flush=True)
    print('TOURNEZ LE KNOB GAUCHE : les pages changent.', flush=True)
    print('CTRL+C pour arreter.', flush=True)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.kill()


if __name__ == '__main__':
    main()
