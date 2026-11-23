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
Prérequis : `pip install -r ../requirements.txt` et le service `DirectOutputService`
(déjà configuré `start= auto` ; si besoin, admin : `sc start DirectOutputService`).

- Service absent → `fipd3` le dit et s'arrête (au lieu de rester bloqué dans
  `DirectOutput_Initialize`, qui ne rend jamais la main dans ce cas).
- FIP pas encore branché → le daemon démarre quand même et se connecte dès
  qu'il apparaît (nouvelle tentative toutes les 10 s).
- FIP débranché/rebranché, service redémarré → la session est reconstruite
  **sans redémarrer le daemon** (`POST /device/reset` pour le demander à la main).
- `POST /exit` = arrêt propre (libère les pages).

## Sécurité
L'API n'a **pas d'authentification** : elle est protégée par le fait qu'elle est
**strictement locale**. Toute requête portant un `Origin` (donc venant d'une page
web, y compris `null`) ou un `Host` non loopback (DNS rebinding) est refusée —
sinon une page web pourrait réécrire `/bindings`, donc injecter des touches.
`/screen?url=` n'accepte que du `http(s)`. Ne l'exposez pas à travers un proxy.

## API
| Endpoint | Effet |
|---|---|
| `POST /screen` (corps = PNG/JPG/BMP/WEBP) | Affiche l'image (320×240 auto) |
| `POST /screen?url=https://...` | Affiche une image téléchargée (http/https uniquement) |
| `POST /video` (fichier, `X-Filename: x.mp4`) | Joue une vidéo (S1 = stop) |
| `POST /ui` | Interface intégrée (horloge/système/réseau) — met fin à image/vidéo |
| `POST /apps/reload` (`{"app":"doom"}` / `{"all":true}`) | Recharge `apps/*.py` à chaud : **seuls les fichiers modifiés** par défaut |
| `GET/POST /app/<name>` | Endpoint propre à une app (http_get/http_post) |
| `GET /buttons` | SSE boutons/molettes |
| `GET /events` | Derniers événements JSON |
| `GET /apps` · `POST /page {"page":N}` | Liste des pages / change de page |
| `POST /led {"index":1..8,"on":bool}` | LED (ou `{"scan":true}`) |
| `GET/POST /bindings` · `GET /keys` | Raccourcis clavier (validés) / touches reconnues |
| `POST /device/reset` · `POST /exit` | Refait la session DirectOutput / arrêt propre |
| `GET /` | Page d'état |

Molette gauche = changer de page (mode ui). Masques HID : S1..S6 = 0x20..0x400,
R_CW/CCW = 0x02/0x04, L_CW/CCW = 0x10/0x08 (confirmés matériel 19/11).

## Tests (aucun FIP nécessaire)
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v     # sans matériel : DirectOutput.dll est simulée
ruff check .                                  # lint (règles et archives de RE exclues : ruff.toml)
```
Les deux tournent à chaque push (`.github/workflows/ci.yml`). `import fipd3` n'a aucun effet de
bord : la DLL est chargée par `Daemon.start()` et l'instance `D` créée par `main()`.

**Sur le vrai FIP** : `tests/hardware/test_recuperation.ps1` (administrateur, une seule
élévation) redémarre deux fois le service Logitech et vérifie que le daemon se rétablit seul ;
`-SansRedemarrage` vérifie le script sans élévation. `GET /apps` expose `ok_age`
(secondes depuis la dernière image acceptée) et `recoveries`.

## Ce qui a été découvert (RE 19/11/2026)
- **Reprise après perte de session (23/11, validée sur le matériel : service
  Logitech redémarré deux fois, affichage revenu en 6,1 s puis 6,8 s)**. Ce qui
  se passe VRAIMENT quand le service redémarre, appris en le faisant :
  1. service arrêté → tous les appels rendent `E_UNEXPECTED` (8000FFFF) ;
  2. `Deinitialize` puis `Initialize` rend `E_FAIL` quelques secondes (le service
     est « Running » mais pas encore prêt) → on réessaie toutes les 2 s ;
  3. `Enumerate` retourne alors l'**ancien périphérique — un handle mort,
     `E_HANDLE` (80070006) à chaque appel — EN PLUS du nouveau** (et un handle
     mort de plus à chaque redémarrage) ;
  4. il faut donc essayer chaque handle et garder le premier qui **accepte une
     image** : `_init_stack` ne dit « OK » qu'après une image acceptée (la
     première version se déclarait « rétablie » sur le handle mort, pour toujours).
  En pratique : `E_PAGENOTACTIVE` (la page n'est plus courante) → auto-guérison
  par ré-activation ; `E_UNEXPECTED`/`E_HANDLE` (session morte) → reconstruction
  immédiate. On ne tente `Initialize` que service `RUNNING` (sinon il attend son
  pipe indéfiniment) ; gardien de 40 s en thread séparé ; tentatives toutes les
  2 s pendant 30 s puis toutes les 10 s ; la page affichée redevient courante
  (pas de flash d'horloge). Un débranchement physique passe par le même chemin
  (non testé en débranchant).
- **Auto-guérison (20/11)** : le firmware peut retomber sur son écran home
  (cycle interne page N ↔ home ↔ page 1, boutons ↑/↓) → tous les SetImage
  échouent en `E_PAGENOTACTIVE` et l'écran restait figé sur Saitek. Le
  keepalive compte maintenant les échecs et re-active la page courante
  (RemovePage+AddPage+redraw) après 2 cycles ratés.
- **Architecture Logitech** : `DirectOutput.dll` = simple client de **pipes nommés**
  vers `DirectOutputService.exe` (SYSTEM). `Initialize` bloque indéfiniment si le
  service ne tourne pas (d'où le pré-check de l'état du service au démarrage).
  Le service héberge `DirectOutputSaiFip.dll` (plugin device).
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
- LEDs : elles marchent **via la page COURANTE** (règle `E_PAGENOTACTIVE`) —
  1-6 = boutons latéraux, 7-8 = LED du bas. (L'appel brut hors page active
  renvoie E_FAIL, d'où la première conclusion, fausse, « non câblées ».)
- `DirectOutputDriver_GetSerialNumber(h, wchar*, size)` — 3 args (l'ancien
  `fip_native.serial()` à 2 args segfaultait).
- SPAD : licence expirée → inutilisable. Son support FIP = extension non obfusquée
  `ext\Saitek.FIP.dll` (UI WPF) + device raw dans SPAD.neXt.Devices.dll (obfusqué).
  SPAD streame des images rendues côté hôte (pas de serveur embarqué).

## Applications fipui — 15 pages (22/11)
**Intégrées** : Horloge, Systeme, Reseau, TEST boutons, JOURNAL (lecture
molette droite), DEMO FIPUI (liste défilante viewport-relative). NB : le FIP
n'a PAS de bouton select physique (0x01). (REFLEX, mini-jeu taupes d'origine,
a été remplacé par DOOM le 22/11.)

**Apps tierces** (`apps/*.py`, chargées au démarrage + `POST /apps/reload`
à chaud — seuls les fichiers modifiés sont reconstruits, les autres gardent
leur état ; un fichier modifié est reconnu par son contenu et compilé depuis
la source, jamais repris d'un `.pyc` de la version d'avant ; un fichier cassé
laisse l'ancienne version en service ; un module
partagé (`charte`) modifié impose de tout reconstruire, `uiatabs`/`fipui`/
`keysend`/`fipd3` modifiés = redémarrage requis, signalé dans la réponse) :
une classe `fipui.App` avec `name` != 'app' et pas `abstract` ;
option `render_full(img)` pour coller des images ; option `http_get/http_post`
pour exposer `/app/<name>` ; **`self.poll(fn, interval)`** pour un travail de
fond qui ne tourne QUE pendant que la page est affichée.

- **FENETRES** — toutes les **fenêtres ouvertes** (EnumWindows : z-order,
  processus, états premier plan/minimisée/toujours devant) + tous les
  **onglets Chrome/Edge** via **UI Automation** (`uiatabs.py`, aucune
  installation ni extension). Layout 30/70 : keycaps contextuels de l'entrée
  sélectionnée (molette droite = curseur) — fenêtre : Focus/Minimiser/Devant/
  Fermer 2×/Tuer 2×/Copier titre ; onglet : **Activer** (bascule UIA)/
  Copier titre/Copier URL (onglet actif)/Google/Volume. Les confirmations en
  deux appuis sont attachées à **leur** cible (fenêtre ou processus). Limites
  UIA : onglets hors bande visible virtualisés absents, URL de l'onglet actif
  uniquement.
- **CONTEXTE** — page caméléon : s'adapte à la **fenêtre/onglet actif**.
  **Layout 2 colonnes 30/70** (keycaps pleine hauteur alignées boutons /
  panneau d'infos). Contextes : **onglet GitHub** → fiche repo (avatar,
  stats, runs, PR, description ; S1-S6 = Issues/PRs/Actions/Repo/URL/Clone) ;
  **onglet YouTube** → miniature + titre + chaîne (oEmbed, Lecture/Pause,
  copies, Google, volume) ; **onglet Gmail** → badge messages non lus (titre), adresse,
  nouveau message (compose) ; **page GitHub PR/Issue** → `gh pr/issue view` :
  état, auteur, +additions/−dels, reviews, commentaires, titre ;
  **modèle HuggingFace** → API publique : likes, downloads, pipeline,
  librairie, `huggingface-cli download` copié en 1 bouton ; **Search Console** → le FIP **lit la page affichée**
  (arbre d'accessibilité, exécution dans le thread UIA dédié) et affiche
  les 4 métriques : Clics / Impressions / CTR / Position — lecture **une fois
  à l'arrivée sur la page** (pas de MAJ auto), S3 copie les 4 valeurs, **S4 = Relire** ;
  **EdiSEO** → pipeline complet lu sur l'écran (entonnoir, pools LLM avec ETA,
  crédits Serper), lu une fois par page, S1-S3 copient, S4 = Relire ;
  **recherche Google** → requête + **résultat de
  calcul local** (AST sûr, ex. 3*1.25 = 3.75 affiché sur le FIP) ou
  **réponse instantanée DuckDuckGo** (réponse directe, sujet + extrait +
  image) pour les requêtes normales + Lucky / Images / Actualités ;
  **Wikipédia** (fr et en) → vignette + description + **résumé défilant à la molette**
  (API REST summary), bascule EN/FR, images ; **chat IA** (z.ai, ChatGPT, Claude, Le Chat) →
  conversation + accueil/nouvelle discussion ; **onglet web**
  → favicon + domaine + titre + zone **PAGE (lecture écran)** : quelques
  lignes du contenu lues sur la page (une fois par URL) +
  URL (copies, Google, domaine, volume) ; **app Windows** → fiches par
  application : **Explorateur** (chemin réel via COM Shell.Application,
  nb d'éléments, espace libre ; copier chemin / chemin unix / terminal ici
  `wt -d` / parent Navigate2 / actualiser ; **sélection suivie + actions par
  type de fichier** : image → miniature + zoom plein écran, vidéo → lecture
  sur l'écran du FIP, audio → ffplay, `.py` → exécution (**double appui**),
  `.zip` → contenu, VS Code), **VS Code** (fichier + workspace),
  **Terminal** (cwd du titre), **VLC** (média, pause/stop/muet) ; sinon fiche
  générique (Copier titre/Toujours devant/Minimiser/Tuer 2×/Redémarrer 2×/
  Chemin exe — le « Tuer 2× » est lié à la fenêtre armée). Onglets et URL via
  **UIA** (`uiatabs.py`), repli GitHub par titre sans UIA.
  `GET /app/contexte` = contexte courant. Rendu **charte
  graphique** (`charte.py` : palette GitHub Dark, `panel`/`keycap`/`pill`/
  `ico_*`/`letter_avatar`/`ago` — réutilisable par toutes les apps).
- **DOOM** 🆕 — FPS raycasting maison (160 rayons, murs ombrés par
  distance/face + joints de briques, sprites billboard clippés contre le
  z-buffer, fusil a pompe avec flash et recul, medikits/munitions, vagues
  sans fin : niveau = +démons, +vitesse, +25 PV, +10 cartouches). Molette
  gauche = tourner (capturée sur cette page), molette droite =
  avancer/reculer, S1 = tirer, S2/S3 = pas de côté, S6 = marche auto,
  LEDs 1-6 = santé.
- **CAMERAS** — flux vidéo MJPEG/RTSP plein écran, liste sur S1-S6, molette
  droite = caméra suivante ; flux ajoutés via `POST /app/cameras`
  (`apps/cameras.json`)
- **DECK** — pages de boutons génériques JSON (`apps/deck.json`, rechargé à
  chaud) : actions `key` (keysend), `url`, `http`, `run`, `alert`
- **CI GITHUB** — statut GitHub Actions de vos repos (`gh`, auto-seed dans
  `apps/github.json`), points vert/rouge/bleu, bouton = ouvrir le run,
  alerte quand un run **passe** en échec (y compris vu « en cours » avant).
  Interrogé seulement pendant que la page est affichée.
- **LECTURE** — Now Playing : source SMTC (Spotify/navigateur…) **ou VLC**
  (VLC 3.x n'a pas de SMTC → interface Web de VLC : lancer une fois
  `activer_vlc_web.bat`, port 8080 mot de passe `fipd`, **écoutée sur
  127.0.0.1 uniquement**, config `apps/vlc.json`).
  Pochette en fond (SMTC), progression ; S1 pause, S2/S3 morceau/chaîne,
  S4 source, S5/S6 volume, molette droite = seek ±10 s (désactivé sur flux
  direct, durée inconnue) ; LED 7 = en lecture
- **TACHES** — top 6 processus (tri CPU/RAM/DISQUE à la molette droite),
  kill sur double-appui **lié au processus** (la liste est retriée entre
  les deux appuis), processus système protégés. **Un seul instantané système
  par passe** (les appels psutil par processus prennent chacun un instantané
  complet : ~10 ms × 3 × N processus, soit des secondes).
- **RADIO** — webradio : catalogue auto (radio-browser.info, stations FR,
  `apps/radio.json`), molette droite = tuner, S1-S6 = presets, titre ICY en
  direct via ffplay (`POST /app/webradio` pour jouer/ajouter à distance)

Le daemon gère : règle page courante, thread d'affichage unique, keepalive
(0,3 s si alerte active), pagination, bindings (l'app cède un contrôle bindé),
`on_enter` appelé **une seule fois** par séjour sur la page, retour LED des
boutons qui rend à chaque LED l'état que l'app lui donne.
Widgets fipui : `draw_header`, `draw_menu`, `draw_bar`, `font(size)` (cache),
`poll()`, `alert()` (bandeau ajusté à l'écran ; en attente si la page n'est
pas affichée), `clipboard_set()`.
**LEDs** : 1-6 = boutons latéraux, **7-8 = LED du bas** (libres ; 7 =
« lecture en cours » sur LECTURE, 8 = clignotement d'alerte). Une app ne
peut allumer ses LEDs QUE sur sa propre page (sinon un thread de fond les
allumait sur la page affichée — corrigé 20/11).
API : GET /apps, POST /page {"page":N}.

**keysend** (raccourcis clavier opt-in) : les lettres, chiffres et signes
sont résolus par la **disposition clavier active** — `a` est la touche *a*
en AZERTY comme en QWERTY (avant, les scancodes étaient codés en QWERTY :
`q`→A, `a`→Q, `z`→W…) ; `win` porte l'indicateur étendu (sans lui, 0x5B
n'est pas la touche Windows) ; les touches sont relâchées en ordre inverse.

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
- `fipui.py` — framework d'apps (rendu, événements, LEDs, alertes, poll)
- `uiatabs.py` — onglets/URL des navigateurs par UI Automation
- `keysend.py` — envoi de touches (SendInput), conscient de la disposition
- `charte.py` — charte graphique partagée
- `apps/` — les 9 apps tierces (hot-load)
- `fip.py` — pilote pur bulk+HID (RE 04/11) — base pour Linux
- `do_host.py` — client standard minimal (test une fois le service lancé)
- `target2.py` — Create DLL direct sans service (boutons à 0 : manque AddPage actif)
- `gen_screens.py` — captures d'écran du FIP pour la doc (`../docs/`), sans matériel
- `../tests/` — suite de tests sans matériel · `../proxy/` — DLL proxy + log de déploiement
- `../disas_*.py` — outillage RE (capstone/pefile) · `../cecil/` — dumps IL (Mono.Cecil)
