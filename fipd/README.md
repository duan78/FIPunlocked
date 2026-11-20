# FIPd v3 — Flight Instrument Panel : écran + boutons via la pile officielle

**Validé le 19/11/2026** : affichage ET boutons/molettes fonctionnels, sans SPAD ni licence.
Architecture : `fipd3.py` → `DirectOutput.dll` (client pipes) → `DirectOutputService`
(service Logitech officiel, installé et en démarrage automatique).

## Démarrage
```bash
cd fipd
python fipd3.py       # -> http://127.0.0.1:8199
```
ou double-clic sur `demarrage.bat` (l'ancien orchestrateur SPAD est obsolète).
Prérequis unique : le service `DirectOutputService` (déjà configuré `start= auto`).
Si besoin (admin) : `sc start DirectOutputService`.

## API
| Endpoint | Effet |
|---|---|
| `POST /screen` (corps = PNG/JPG/BMP/WEBP) | Affiche l'image (320×240 auto) |
| `POST /screen?url=https://...` | Affiche une image téléchargée |
| `POST /video` (fichier, `X-Filename: x.mp4`) | Joue une vidéo (S1 = stop) |
| `POST /ui` | Interface intégrée (horloge/système/réseau) |
| `POST /apps/reload` | Recharge les apps tierces `apps/*.py` à chaud |
| `GET/POST /app/<name>` | Endpoint propre à une app (http_get/http_post) |
| `GET /buttons` | SSE boutons/molettes |
| `GET /events` | Derniers événements JSON |
| `GET /` | Page d'état |

Molette gauche = changer de page (mode ui). Masques HID : S1..S6 = 0x20..0x400,
select=0x01, R_CW/CCW=0x02/0x04, L_CW/CCW=0x10/0x08 (confirmés matériel 19/11).

## Ce qui a été découvert (RE 19/11/2026)
- **Auto-guérison (20/11)** : le firmware peut retomber sur son écran home
  (cycle interne page N ↔ home ↔ page 1, boutons ↑/↓) → tous les SetImage
  échouent en `E_PAGENOTACTIVE` et l'écran restait figé sur Saitek. Le
  keepalive compte maintenant les échecs et re-active la page courante
  (RemovePage+AddPage+redraw) après 2 cycles ratés.
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

## Applications fipui — 13 pages (20/11 soir)
**Intégrées** : Horloge, Systeme, Reseau, TEST boutons, JOURNAL (lecture
molette droite), DEMO FIPUI (liste défilante viewport-relative) et REFLEX
(mini-jeu taupes). NB : le FIP n'a PAS de bouton select physique (0x01).

**Apps tierces** (`apps/*.py`, chargées au démarrage + `POST /apps/reload`
à chaud ; une classe `fipui.App` avec `name` != 'app' et pas `abstract` ;
option `render_full(img)` pour coller des images ; option `http_get/http_post`
pour exposer `/app/<name>`) :
- - **FENETRES** — toutes les **fenêtres ouvertes** (EnumWindows : z-order,
  processus, états premier plan/minimisée/toujours devant) + tous les
  **onglets Chrome/Edge** via **UI Automation** (`uiatabs.py`, aucune
  installation ni extension). Layout 30/70 : keycaps contextuels de l'entrée
  sélectionnée (molette droite = curseur) — fenêtre : Focus/Minimiser/Devant/
  Fermer 2×/Tuer 2×/Copier titre ; onglet : **Activer** (bascule UIA)/
  Copier titre/Copier URL (onglet actif)/Google/Volume. Limites UIA :
  onglets hors bande visible virtualisés absents, URL de l'onglet actif
  uniquement.
- **CONTEXTE** — page caméléon : s'adapte à la **fenêtre/onglet actif**.
  **Layout 2 colonnes 30/70** (keycaps pleine hauteur alignées boutons /
  panneau d'infos). Contextes : **onglet GitHub** → fiche repo (avatar,
  stats, runs, PR, description ; S1-S6 = Issues/PRs/Actions/Repo/URL/Clone)
  ; **onglet YouTube** → miniature + titre + chaîne (oEmbed, Lecture/Pause,
  copies, Google, volume) ; **onglet Gmail** → badge messages non lus (titre), adresse,
  nouveau message (compose) ; **page GitHub PR/Issue** → `gh pr/issue view` :
  état, auteur, +additions/-deletions, reviews, commentaires, titre ;
  **modèle HuggingFace** → API publique : likes, downloads, pipeline,
  librairie, `huggingface-cli download` copié en 1 bouton ; **onglet web**
  → favicon + domaine + titre +
  URL (copies, Google, domaine, volume) ; **app Windows** → fiches par
  application : **Explorateur** (chemin réel via COM Shell.Application,
  nb d'éléments, espace libre ; copier chemin / chemin unix / terminal ici
  `wt -d` / parent Navigate2 / actualiser ; **sélection suivie + actions par
  type de fichier** : image → miniature + zoom plein écran, vidéo → lecture
  sur l'écran du FIP, audio → ffplay, `.py` → exécution, `.zip` → contenu,
  VS Code), **VS Code** (fichier + workspace),
  **Terminal** (cwd du titre), **VLC** (média, pause/stop/muet) ; sinon fiche
  générique (Copier titre/Toujours devant/Minimiser/Tuer 2×/Redémarrer 2×/
  Chemin exe). Onglets et URL via **UIA** (`uiatabs.py`), repli GitHub par
  titre sans UIA. `GET /app/contexte` = contexte courant. Rendu **charte
  graphique** (`charte.py` : palette GitHub Dark, `panel`/`keycap`/`pill`/
  `ico_*`/`letter_avatar`/`ago` — réutilisable par toutes les apps).
- **CAMERAS** — flux vidéo MJPEG/RTSP plein écran, liste sur S1-S6, molette
  droite = caméra suivante ; flux ajoutés via `POST /app/cameras`
  (`apps/cameras.json`)
- **DECK** — pages de boutons génériques JSON (`apps/deck.json`, rechargé à
  chaud) : actions `key` (keysend), `url`, `http`, `run`, `alert`
- **CI GITHUB** — statut GitHub Actions de vos repos (`gh`, auto-seed dans
  `apps/github.json`), points vert/rouge/bleu, bouton = ouvrir le run,
  alerte si échec CI
- **LECTURE** — Now Playing : source SMTC (Spotify/navigateur…) **ou VLC**
  (VLC 3.x n'a pas de SMTC → interface Web de VLC : lancer une fois
  `activer_vlc_web.bat`, port 8080 mot de passe `fipd`, config `apps/vlc.json`).
  Pochette en fond (SMTC), progression ; S1 pause, S2/S3 morceau/chaîne,
  S4 source, S5/S6 volume, molette droite = seek ±10 s (désactivé sur flux
  direct, durée inconnue) ; LED 7 = en lecture
- **TACHES** — top 6 processus (tri CPU/RAM/DISQUE à la molette droite),
  kill sur double-appui, processus système protégés
- **RADIO** — webradio : catalogue auto (radio-browser.info, stations FR,
  `apps/radio.json`), molette droite = tuner, S1-S6 = presets, titre ICY en
  direct via ffplay (`POST /app/webradio` pour jouer/ajouter à distance)

Le daemon gère : règle page courante, thread d'affichage unique, keepalive
(0.3 s si alerte active), pagination, bindings (l'app cède un contrôle bindé).
Widgets fipui : `draw_header`, `draw_menu`, `draw_bar`, `font(size)` (cache).
**LEDs** : 1-6 = boutons latéraux, **7-8 = LED du bas** (libres ; 7 =
« lecture en cours » sur LECTURE, 8 = clignotement d'alerte). Une app ne
peut allumer ses LEDs QUE sur sa propre page (sinon un thread de fond les
allumait sur la page affichée — corrigé 20/11).
API : GET /apps, POST /page {"page":N}.

## Reste à faire
- Widgets supplémentaires (sliders, jauges animées).
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
