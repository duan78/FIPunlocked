"""
test_pur_postservice.py — Apres arret du service : le firmware garde-t-il son serveur ?
Test pilote PUR : display (fip.py) + lecteur HID boutons 30 s.
"""
import sys
import threading
import time

import fip

print('== pilote pur, service arrete ==', flush=True)
dev = fip.FIP()
print('Bulk:', dev.path, flush=True)
print('HID :', dev._hid[1] if dev._hid else 'ABSENT', flush=True)

m = dev.ping()
print('PING:', 'ok' if m else 'echec', flush=True)
print('Version:', dev.get_version(), flush=True)

events = []


def on_buttons(state, changed, names):
    line = f'*** BOUTONS state=0x{state:03X} {names}'
    print(line, flush=True)
    events.append(names)


dev.on_buttons = on_buttons

img = b'\x00\x00\xff' * (320 * 240)
r = dev.display(img)
print('Image rouge:', r if r is None else {k: r[k] for k in ('length', 'request_le')}, flush=True)

print('>>> 30 s : APPUYEZ BOUTONS + KNOBS (rappel image /5 s) <<<', flush=True)
t0 = time.time()
while time.time() - t0 < 30:
    if int(time.time() - t0) % 5 == 0:
        dev.display(img)
    time.sleep(0.2)

dev.close()
n = len(events)
print(f'TOTAL: {n} evenements', flush=True)
print('VERDICT:', 'LE SERVEUR SURVIT AU SERVICE — PRISE EN MAIN PURE POSSIBLE' if n else
      'pas d evenements (serveur mort au stop ?)', flush=True)
sys.exit(0 if n else 1)
