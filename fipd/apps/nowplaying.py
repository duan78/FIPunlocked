"""
nowplaying.py — "Now Playing" universel.
Source 1 : SMTC (le "media widget" de Windows) — Spotify, navigateurs, etc.
Source 2 (secours) : interface Web de VLC — VLC 3.x n'a PAS de SMTC (prevu
pour la 4.0) ; activez-la une fois avec activer_vlc_web.bat (port 8080,
mot de passe "fipd", config apps/vlc.json).

Controles :
  S1 = lecture/pause      S2 = suivant      S3 = precedent
  S4 = source suivante (parmi les lecteurs SMTC actifs)
  S5 = volume -           S6 = volume +     (touches systeme)
  molette droite = avance/recul de 10 s dans le morceau
  LED 7 allumee = quelque chose joue
"""
import asyncio
import io
import json
import os
import threading
import time

import fipui
from fipui import App

try:
    from winrt.windows.media.control import \
        GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_SMTC = True
except ImportError:
    HAVE_SMTC = False

SEEK_S = 10              # secondes par cran de molette
POLL_S = 1.0
VLC_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vlc.json')
VLC_DEFAULTS = {'host': '127.0.0.1', 'port': 8080, 'password': 'fipd'}
VLC_RETRY_MAX_S = 30.0    # VLC muet : on espacera les essais (2 s, 4 s, ... 30 s)


def _fmt(s):
    s = max(0, int(s))
    return '{}:{:02d}'.format(s // 60, s % 60)


class NowPlayingApp(App):
    name = 'nowplaying'
    title = 'LECTURE'
    show_footer = False

    def __init__(self, daemon):
        super().__init__(daemon)
        self.info = None            # snapshot SMTC (dict) ou None
        self.error = None
        self.hb = 0                 # battement de coeur du thread de poll
        self._target_id = None      # AUMID de la session pilotee
        self._stop = threading.Event()
        self._aio = None            # boucle asyncio du thread de poll
        self._mgr = None
        self._art_key = None        # (title, artist, album) courant
        self._art = None            # PIL Image pochette (RGB)
        self._cfg = None            # vlc.json en cache (relu si le fichier change)
        self._cfg_t = None
        self._vlc_next = 0.0        # date du prochain essai VLC
        self._vlc_fail = 0
        if HAVE_SMTC:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name='nowplaying-poll')
            self._thread.start()
        else:
            self.error = 'pip install winrt-Windows.Media.Control'

    # ---------- thread de poll SMTC (page affichee seulement) ----------

    def _run(self):
        asyncio.run(self._poll())

    async def _poll(self):
        self._mgr = await _Mgr.request_async()
        self._aio = asyncio.get_running_loop()
        while not self._stop.is_set():
            if not self._shown.is_set():
                self._shown.wait()        # page masquee : aucune requete SMTC ni VLC
                if self._stop.is_set():
                    return
            try:
                await self._refresh()
                self.error = None
            except Exception as e:
                self.error = str(e)
            if self.info is None and time.time() >= self._vlc_next:
                self._refresh_vlc()      # secours : interface Web de VLC
            self.hb += 1
            await asyncio.sleep(POLL_S)

    async def _refresh(self):
        sessions = list(self._mgr.get_sessions())
        ids = [s.source_app_user_model_id or '?' for s in sessions]
        if self._target_id not in ids:
            cur = self._mgr.get_current_session()
            self._target_id = (cur.source_app_user_model_id if cur
                               else (ids[0] if ids else None))
        sess = next((s for s in sessions
                     if s.source_app_user_model_id == self._target_id), None)
        if sess is None:
            self.info = None
            self.set_led(7, False)
            return
        props = await sess.try_get_media_properties_async()
        tl = sess.get_timeline_properties()
        try:
            st = sess.get_playback_info().playback_status
            st = int(getattr(st, 'value', st))
        except Exception:
            st = 0
        self.info = {
            't': time.time(),
            'title': props.title or '(sans titre)',
            'artist': props.artist,
            'album': props.album,
            'src': self._friendly(self._target_id),
            'sources': [self._friendly(i) for i in ids],
            'pos': tl.position / 1e7,
            'dur': tl.end_time / 1e7,
            'playing': st == 4,
        }
        self.set_led(7, st == 4)
        # pochette (cache par morceau)
        key = (props.title, props.artist, props.album)
        if key != self._art_key:
            self._art_key = key
            self._art = None
            if props.thumbnail:
                data = await self._bytes(props.thumbnail)
                if data:
                    try:
                        from PIL import Image
                        self._art = Image.open(io.BytesIO(data)).convert('RGB')
                    except Exception:
                        pass

    @staticmethod
    async def _bytes(ref):
        """IRandomAccessStreamReference -> octets (pywinrt : plusieurs chapons)."""
        try:
            stream = await ref.open_read_async()
            size = int(stream.size)
            try:
                return bytes(stream.read(size))
            except Exception:
                pass
            from winrt.windows.storage.streams import DataReader
            dr = DataReader(stream)
            await dr.load_async(size)
            buf = dr.read_buffer(size)
            return bytes(memoryview(buf))
        except Exception:
            return None

    @staticmethod
    def _friendly(aumid):
        """'chrome.exe!App' -> 'Chrome' ; 'Spotify.exe' -> 'Spotify'."""
        s = (aumid or '?').split('!')[0]
        s = s[:-4] if s.lower().endswith('.exe') else s
        return s[:14].capitalize() if s and s != '?' else '?'

    # ---------- secours : interface Web de VLC ----------

    def _load_vlc_cfg(self):
        try:
            m = os.path.getmtime(VLC_CFG)
        except OSError:
            m = None
        if self._cfg is not None and m == self._cfg_t:     # relu seulement si modifie
            return dict(self._cfg)
        cfg = dict(VLC_DEFAULTS)
        try:
            if m is None:
                with open(VLC_CFG, 'w', encoding='utf-8') as f:
                    json.dump(VLC_DEFAULTS, f, indent=2)
            else:
                with open(VLC_CFG, encoding='utf-8') as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
        except Exception:
            pass
        self._cfg, self._cfg_t = dict(cfg), m
        return cfg

    def _vlc_url(self, cfg):
        return 'http://{}:{}/requests/status.json'.format(
            cfg.get('host', '127.0.0.1'), cfg.get('port', 8080))

    def _refresh_vlc(self):
        """Appelle sans session SMTC : lit status.json (blocant court)."""
        st = None
        try:
            import requests
            cfg = self._load_vlc_cfg()
            auth = ('', cfg['password']) if cfg.get('password') else None
            r = requests.get(self._vlc_url(cfg), auth=auth, timeout=1.2)
            if r.status_code == 200:
                st = r.json()
        except Exception:
            pass
        if not st:
            self.info = None                 # plus aucune source
            self.set_led(7, False)
            self._vlc_fail += 1              # VLC ne repond pas : essais espaces
            self._vlc_next = time.time() + min(
                VLC_RETRY_MAX_S, 2.0 ** min(5, self._vlc_fail))
            return
        self._vlc_fail, self._vlc_next = 0, 0.0
        self._art = None                     # pas de pochette via VLC
        self._art_key = None
        meta = ((st.get('information') or {}).get('category')
                or {}).get('meta', {})
        title = (meta.get('now_playing') or meta.get('title')
                 or st.get('title') or 'VLC')
        artist = None
        if ' - ' in title and meta.get('now_playing'):
            artist, title = title.split(' - ', 1)
        self.info = {
            't': time.time(),
            'title': str(title)[:80] or 'VLC',
            'artist': artist,
            'album': meta.get('album'),
            'src': 'VLC',
            'sources': ['VLC'],
            'pos': float(st.get('time') or 0),
            'dur': float(st.get('length') or 0),
            'playing': st.get('state') == 'playing',
            'vlc': True,
        }
        self.set_led(7, self.info['playing'])

    def _vlc_cmd(self, command, val=None):
        cfg = self._load_vlc_cfg()

        def go():
            try:
                import requests
                auth = ('', cfg['password']) if cfg.get('password') else None
                params = {'command': command}
                if val is not None:
                    params['val'] = val
                requests.get(self._vlc_url(cfg), params=params, auth=auth,
                             timeout=1.5)
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()

    # ---------- actions (depuis le thread boutons -> planifiees dans la boucle) ----------

    def _async(self, coro_fn):
        loop = self._aio
        if loop and not self._stop.is_set():
            asyncio.run_coroutine_threadsafe(coro_fn(), loop)

    def _sess(self):
        for s in self._mgr.get_sessions():
            if s.source_app_user_model_id == self._target_id:
                return s
        return None

    def on_event(self, name, pressed):
        if not pressed:
            return
        import keysend
        vlc = bool(self.info and self.info.get('vlc'))
        if name == 'S1':
            if vlc:
                self._vlc_cmd('pl_pause')
            else:
                self._async(lambda: self._act('try_toggle_play_pause_async'))
        elif name == 'S2':
            if vlc:
                self._vlc_cmd('pl_next')
            else:
                self._async(lambda: self._act('try_skip_next_async'))
        elif name == 'S3':
            if vlc:
                self._vlc_cmd('pl_previous')
            else:
                self._async(lambda: self._act('try_skip_previous_async'))
        elif name == 'S4':
            if vlc:
                self.alert('source: VLC (web)')
            else:
                self._cycle_source()
        elif name == 'S5':
            keysend.tap('volumedown')
            keysend.tap('volumedown')
        elif name == 'S6':
            keysend.tap('volumeup')
            keysend.tap('volumeup')
        elif name == 'right_cw':
            self._seek(SEEK_S)
        elif name == 'right_ccw':
            self._seek(-SEEK_S)

    async def _act(self, method):
        s = self._sess()
        if s:
            await getattr(s, method)()

    def _cycle_source(self):
        ids = [i for i in
               (getattr(s, 'source_app_user_model_id', '?')
                for s in self._mgr.get_sessions())]
        if len(ids) < 2:
            self.alert('une seule source')
            return
        i = ids.index(self._target_id) if self._target_id in ids else 0
        self._target_id = ids[(i + 1) % len(ids)]
        self.alert('source: ' + self._friendly(self._target_id))
        self._async(lambda: self._refresh())

    def _seek(self, delta):
        info = self.info
        if not info or info['dur'] <= 0:
            return
        pos = self._now_pos(info) + delta
        pos = max(0.0, min(info['dur'] - 1, pos))
        if info.get('vlc'):
            self._vlc_cmd('seek', pos)         # secondes absolues
        else:
            ticks = int(pos * 1e7)

            async def go():
                s = self._sess()
                if s:
                    await s.try_change_playback_position_async(ticks)
            self._async(go)
        info['pos'] = pos                      # retour immediat a l'ecran
        info['t'] = time.time()

    @staticmethod
    def _now_pos(info):
        pos = info['pos']
        if info['playing']:
            pos += time.time() - info['t']
        return min(pos, info['dur'] or pos)

    # ---------- rendu ----------

    def http_get(self):
        info = self.info
        return {
            'source': info['src'] if info else None,
            'error': self.error,
            'hb': self.hb, 'alive': bool(self._thread and self._thread.is_alive()),
            'info': ({k: info[k] for k in
                      ('title', 'artist', 'album', 'src', 'pos', 'dur', 'playing')}
                     if info else None),
        }

    def on_leave(self):
        self.d.set_led(7, False)

    def render_full(self, img):
        from PIL import Image, ImageDraw, ImageEnhance
        d = ImageDraw.Draw(img)
        F_S = fipui.font(20)
        F_XS = fipui.font(14)
        if self.error and not self.info:
            d.text((160, 100), 'LECTURE', font=fipui.font(40), fill=fipui.DIM,
                   anchor='mm')
            d.text((160, 150), self.error[:38], font=F_XS, fill=fipui.RED,
                   anchor='mm')
            return
        info = self.info
        if not info:
            d.text((160, 90), 'Aucune lecture', font=fipui.font(30),
                   fill=fipui.DIM, anchor='mm')
            d.text((160, 130), 'Spotify/navigateur : lecteur SMTC', font=F_XS,
                   fill=fipui.DIM, anchor='mm')
            d.text((160, 150), 'VLC : lancez activer_vlc_web.bat (1 fois)',
                   font=F_XS, fill=(120, 170, 240), anchor='mm')
            d.text((160, 190), 'S1 lecture - S2/S3 morceau - S4 source', font=F_XS,
                   fill=(90, 95, 110), anchor='mm')
            d.text((160, 208), 'S5/S6 volume - molette = avance 10 s', font=F_XS,
                   fill=(90, 95, 110), anchor='mm')
            return
        # pochette en fond (cover 320x150, assombrie) ou bandeau neutre
        top = Image.new('RGB', (320, 150), (24, 28, 40))
        if self._art is not None:
            art = self._art.copy()
            ratio = max(320 / art.width, 150 / art.height)
            art = art.resize((int(art.width * ratio) + 1, int(art.height * ratio) + 1))
            x = (art.width - 320) // 2
            y = (art.height - 150) // 2
            top = ImageEnhance.Brightness(art.crop((x, y, x + 320, y + 150))).enhance(0.72)
        img.paste(top, (0, 0))
        # etat / source
        dot = fipui.GREEN if info['playing'] else fipui.DIM
        d.ellipse((10, 10, 20, 20), fill=dot)
        d.text((28, 15), info['src'][:20], font=F_XS, fill=fipui.FG, anchor='lm')
        if len(info['sources']) > 1:
            d.text((312, 15), 'S4: source', font=F_XS, fill=(160, 165, 180),
                   anchor='rm')
        # panneau bas : titre / artiste / progression
        d.rectangle((0, 150, 319, 239), fill=(12, 14, 22))
        title = info['title']
        if d.textlength(title, font=F_S) > 300:
            while title and d.textlength(title + '...', font=F_S) > 300:
                title = title[:-1]
            title += '...'
        d.text((12, 172), title, font=F_S, fill=fipui.FG, anchor='lm')
        sub = ' - '.join(x for x in (info['artist'], info['album']) if x) or ' '
        if d.textlength(sub, font=F_XS) > 296:
            while sub and d.textlength(sub + '...', font=F_XS) > 296:
                sub = sub[:-1]
            sub += '...'
        d.text((12, 196), sub, font=F_XS, fill=fipui.DIM, anchor='lm')
        pos, dur = self._now_pos(info), info['dur']
        if dur > 0:
            fipui.draw_bar(d, 12, 214, 268, 224, pos / dur, fipui.BLUE)
            d.text((10, 232), _fmt(pos), font=F_XS, fill=fipui.DIM, anchor='lm')
            d.text((310, 232), _fmt(dur), font=F_XS, fill=fipui.DIM, anchor='rm')
        else:
            d.text((12, 219), 'flux direct (pas de duree)', font=F_XS,
                   fill=(90, 95, 110), anchor='lm')
