# FIPd v3 — Flight Instrument Panel : écran + boutons via la pile officielle

**Validé le 19/11/2026** : affichage ET boutons/molettes fonctionnels, sans SPAD ni licence.
Architecture : `fipd3.py` → `DirectOutput.dll` (client pipes) → `DirectOutputService`
(service Logitech officiel, installé et en démarrage automatique).

## Démarrage
```bash
cd fipd
python fipd3.py       # -> http://127.0.0.1:8199
```
Prérequis unique : le service `DirectOutputService` (déjà configuré `start= auto`).
Si besoin (admin) : `sc start DirectOutputService`.

## API
| Endpoint | Effet |
|---|---|
| `POST /screen` (corps = PNG/JPG/BMP/WEBP) | Affiche l'image (320×240 auto) |
| `POST /screen?url=https://...` | Affiche une image téléchargée |
| `POST /video` (fichier, `X-Filename: x.mp4`) | Joue une vidéo (S1 = stop) |
| `POST /ui` | Interface intégrée (horloge/système/réseau) |
| `GET /buttons` | SSE boutons/molettes |
| `GET /events` | Derniers événements JSON |
| `GET /` | Page d'état |

Molette gauche = changer de page (mode ui). Masques HID : S1..S6 = 0x20..0x400,
select=0x01, R_CW/CCW=0x02/0x04, L_CW/CCW=0x10/0x08 (confirmés matériel 19/11).

## Ce qui a été découvert (RE 19/11/2026)
- **Architecture Logitech** : `DirectOutput.dll` = simple client de **pipes nommés**
  vers `DirectOutputService.exe` (SYSTEM). `Initialize` bloque indéfiniment si le
  service ne tourne pas. Le service héberge `DirectOutputSaiFip.dll` (plugin device).
- **Séquence d'init du service** (DLL proxy, `proxy_log_copie.txt`) :
  `Create(bulk_path)` → `RegisterSoftButtonCallback` → `DefaultPageRegisterForceActivateCallback`
  → `DefaultPageActivate(1)`. **Aucun StartServer, aucun upload** — l'API
  `StartServer(fichier)` du SDK existe mais n'est pas utilisée par le service.
- `Create()` du plugin fait TOUT le travail wire : open bulk + ioctl 0x220004 (série)
  + GET_VERSION + open HID + ioctls HidD + boucle ReadFile (boutons).
  (`startserver_session.log` = capture complète de cette séquence, flags=1 display).
- **Le firmware émet l'HID seulement avec une page ACTIVE** (`AddPage(0, FLAG_SET_AS_ACTIVE)`)
  et un keepalive d'image (sinon retour écran d'accueil Saitek, ~2-3 s).
- `DefaultPageActivate` ne touche pas le fil : flag interne + `SetEvent` worker.
- LEDs : `SetLed` → E_FAIL via l'API (probablement non câblées sur l'A2AE).
- `DirectOutputDriver_GetSerialNumber(h, wchar*, size)` — 3 args (l'ancien
  `fip_native.serial()` à 2 args segfaultait).
- SPAD : licence expirée → inutilisable. Son support FIP = extension non obfusquée
  `ext\Saitek.FIP.dll` (UI WPF) + device raw dans SPAD.neXt.Devices.dll (obfusqué).
  SPAD streame des images rendues côté hôte (pas de serveur embarqué).

## Applications fipui (20/11)
Chaque page = une `fipui.App` (fipui.py) : `render(d)`, `on_event(name, pressed)`,
`on_enter/on_leave`, `tick()`, LEDs persistantes (`set_led` re-aplique a l'entree,
les tampons pages etant perdus au RemovePage+AddPage), alertes (`alert()` =
bandeau rouge clignotant + LED 8, ~2 Hz). Widgets : `draw_header`, `draw_menu`,
`draw_bar`. Le daemon gere : regle page courante, thread d'affichage unique,
keepalive (0.3 s si alerte active), pagination, bindings (l'app cede un controle
binde). 6 pages integrees dont la DEMO FIPUI (menu molette + toggles LED + alerte).
API : GET /apps, POST /page {"page":N}.

## Reste à faire
- **Applis** : chargement d'apps tierces depuis un dossier apps/ (hot-load),
  widgets supplementaires (sliders, jauges animees).
- **Init pur (pour Linux)** : capturer les bytes wire exacts de `AddPage(0,ACTIVE)`
  côté service — proxy DLL amélioré (logger TOUS les args de Create + hooker les
  WriteFile du vrai plugin via IAT) pendant un cycle service+client. Puis :
  `firmware_init()` pur Python dans fip.py (bulk+HID), powercycle, valider sans
  service. Outillage prêt : `proxy/proxy.c` (gcc Strawberry), `deploy_proxy.ps1`.
- Portage Linux : MI_01 via libusb (bulk 44o BE + payload), MI_00 via hidraw.
- Frein connu : Frida ne peut pas s'attacher au service SYSTEM même admin+SeDebug ;
  la DLL proxy est la voie.

## Fichiers
- `fipd3.py` — daemon v3 (pile officielle) ← **celui-ci**
- `fip.py` — pilote pur bulk+HID (RE 04/11) — base pour Linux
- `do_host.py` — client standard minimal (test une fois le service lancé)
- `target2.py` — Create DLL direct sans service (boutons à 0 : manque AddPage actif)
- `../proxy/` — DLL proxy + log de déploiement
- `../disas_*.py` — outillage RE (capstone/pefile)
- `../cecil/` — dumps IL (Mono.Cecil)
