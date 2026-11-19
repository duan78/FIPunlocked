"""
valide_boutons.py — Test autonome des boutons/knobs du FIP via le daemon.

Lancez ce script, puis tournez les knobs et appuyez sur les boutons du panneau
pendant 60 secondes. Le verdict s'affiche en direct.
"""
import json
import time
import urllib.request

URL = 'http://127.0.0.1:8199/events'
DUREE = 60

print('=== Test boutons FIP (60 s) ===')
print('Tournez les knobs + appuyez sur les boutons MAINTENANT !')
t0 = time.time()
vus = []
dernier = 0
while time.time() - t0 < DUREE:
    try:
        evs = json.load(urllib.request.urlopen(URL, timeout=3))
    except Exception as e:
        print('daemon injoignable:', e)
        break
    if len(evs) > len(vus):
        for ev in evs[len(vus):]:
            restant = int(DUREE - (time.time() - t0))
            print(f'  [{restant:2d}s restant] EVENT: {ev["names"]} (state=0x{ev["state"]:03X})')
        vus = evs
    time.sleep(1)

print()
if vus:
    print(f'REUSSI : {len(vus)} evenements recus par le daemon !')
    names = sorted({n for e in vus for n in e["names"]})
    print('Controles detectes :', ', '.join(names))
    print("=> L'interface doit aussi avoir change de page si vous avez tourne le knob gauche.")
else:
    print('AUCUN evenement recupere.')
    print('1. Verifiez que le daemon tourne : curl http://127.0.0.1:8199/')
    print('2. Relancez-le proprement : arretez tout, puis "python run_fipd.py"')
    print('3. Si le firmware est encrasse : powercycle USB du FIP puis relancez le daemon')
