"""Superviseur fipd : redemarre le daemon en cas de crash (avec re-init firmware)."""
import subprocess
import sys
import time

MAX_RESTARTS = 10
for i in range(MAX_RESTARTS):
    print(f'[superviseur] demarrage fipd (essai {i + 1}/{MAX_RESTARTS})...', flush=True)
    p = subprocess.run([sys.executable, 'fipd.py'])
    print(f'[superviseur] fipd termine (code {p.returncode}) — nouvelle tentative dans 3 s', flush=True)
    time.sleep(3)
print('[superviseur] nombre de redemarrages epuise')
