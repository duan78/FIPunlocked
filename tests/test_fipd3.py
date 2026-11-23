"""fipd3 : daemon sans effets de bord a l'import, retour LED, on_enter unique,
modes image/video, reprise apres perte du peripherique, API locale seulement,
reload cible. DirectOutput.dll est remplacee par un faux qui rejoue les
callbacks PageChange du service."""
import ctypes
import importlib.util
import json
import os
import py_compile
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import helpers
import fipd3
import fipui
import uiatabs   # noqa: F401  (module partage present pour reload_apps)

from PIL import Image


def _hr(code, message):
    """L'OSError que leve ctypes sur un HRESULT en echec (winerror SIGNE)."""
    return OSError(None, message, None, ctypes.c_long(code).value)


def _page_not_active():
    return _hr(0xFF040001, 'E_PAGENOTACTIVE')


def _put(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _pass_as_current(pyc, source):
    """Fait passer un .pyc pour a jour alors qu'il compile une ANCIENNE version
    de la source : Python n'y verifie que la date (a la seconde) et la taille,
    remplacees ici par celles de la source actuelle."""
    st = os.stat(source)
    with open(pyc, 'r+b') as f:
        f.seek(8)                      # en-tete : magic, flags, date, taille
        f.write(struct.pack('<II', int(st.st_mtime) & 0xFFFFFFFF,
                            st.st_size & 0xFFFFFFFF))


class FakeDo:
    """DirectOutput.dll factice, avec les TROIS pannes vues sur le materiel :
      service_down : E_UNEXPECTED partout (le service est arrete)
      dead_handles : E_HANDLE pour ces handles (session d'un service redemarre)
      dead         : E_PAGENOTACTIVE (page inactive : tracasse du firmware)"""

    def __init__(self):
        self.calls = []
        self.daemon = None            # recevra les callbacks PageChange
        self.devices = [4242]         # handles retournes par Enumerate
        self.dead_handles = set()
        self.dead = False
        self.service_down = False
        self.init_failures = 0        # prochains Initialize() en echec (E_FAIL)
        self.on_initialize = None     # hook apres un Initialize reussi

    def _check(self, h):
        if self.service_down:
            raise _hr(0x8000FFFF, 'E_UNEXPECTED')
        if h in self.dead_handles:
            raise _hr(0x80070006, 'E_HANDLE')

    def _cb(self, h, page, active):
        if self.daemon is not None:
            self.daemon._on_page(h, page, active, None)

    def DirectOutput_Initialize(self, app):
        self.calls.append(('Initialize', app))
        if self.service_down:
            raise _hr(0x8000FFFF, 'E_UNEXPECTED')      # le service n'accepte rien
        if self.init_failures:
            self.init_failures -= 1
            raise _hr(0x80004005, 'E_FAIL')            # reparti mais pas encore pret
        if self.on_initialize is not None:
            self.on_initialize()
        return 0

    def DirectOutput_Enumerate(self, cb, ctx):
        if not self.dead:                       # service arrete : rien n'est enumere
            for h in self.devices:
                cb(h, ctx)
        return 0

    def DirectOutput_Deinitialize(self):
        self.calls.append(('Deinitialize',))
        return 0

    def DirectOutput_RegisterSoftButtonCallback(self, h, *a):
        self._check(h)
        return 0

    def DirectOutput_RegisterPageCallback(self, h, *a):
        self._check(h)
        return 0

    def DirectOutput_RemovePage(self, h, page):
        self._check(h)
        self.calls.append(('RemovePage', page))
        self._cb(h, page, False)
        return 0

    def DirectOutput_AddPage(self, h, page, name, flags):
        self._check(h)
        if self.dead:
            raise _page_not_active()
        self.calls.append(('AddPage', page, flags))
        if flags & 1:
            self._cb(h, page, True)
        return 0

    def DirectOutput_SetImage(self, h, page, flags, size, buf):
        self._check(h)
        if self.dead:
            raise _page_not_active()
        self.calls.append(('SetImage', page, size))
        return 0

    def DirectOutput_SetLed(self, h, page, index, on):
        self._check(h)
        self.calls.append(('SetLed', page, index, on))
        return 0

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class Counter(fipui.App):
    """Compte ses entrees/sorties de page."""
    name = 'counter'
    title = 'COUNTER'

    def __init__(self, daemon):
        super().__init__(daemon)
        self.enters = self.leaves = 0

    def on_enter(self):
        self.enters += 1

    def on_leave(self):
        self.leaves += 1


def _daemon(fake):
    d = fipd3.Daemon(with_apps=False)
    d.do = fake
    d.hdev = 4242
    fake.daemon = d
    d._entered = d.apps[0]
    d.apps[0]._shown.set()
    return d


class ImportTests(unittest.TestCase):
    def test_import_has_no_side_effect(self):
        """Avant : DLL chargee et Daemon (donc tous ses threads) construit a l'import."""
        self.assertIsNone(fipd3.do)
        self.assertIsNone(fipd3.D)
        self.assertFalse(any(t.name in ('fipd-keepalive',) for t in threading.enumerate()))

    def test_service_state(self):
        st = fipd3.service_state('DirectOutputService')
        self.assertTrue(st is None or st in (
            'STOPPED', 'START_PENDING', 'STOP_PENDING', 'RUNNING', 'CONTINUE_PENDING',
            'PAUSE_PENDING', 'PAUSED'))


class LedFeedbackTests(unittest.TestCase):
    def test_release_gives_the_led_back_to_the_app(self):
        """Au relachement la LED reprenait OFF meme si l'app la veut allumee
        (jauge de sante de DOOM eteinte des le premier tir)."""
        fake = FakeDo()
        d = _daemon(fake)
        app = d.apps[0]
        app.set_led(1, True)                         # l'app revendique la LED 1
        with helpers.quiet():
            d._on_btn(4242, 0x20, None)              # appui S1
            d._on_btn(4242, 0, None)                 # relachement
            d._on_btn(4242, 0x40, None)              # appui S2 (LED 2 non revendiquee)
            d._on_btn(4242, 0, None)                 # relachement
        leds = [c[2:] for c in fake.named('SetLed')]
        self.assertEqual(leds, [(1, True), (1, True), (1, True), (2, True), (2, False)])


class EnterOnceTests(unittest.TestCase):
    def test_knob_navigation_enters_the_page_once(self):
        """Le callback PageChange ET le thread d'affichage appelaient on_enter."""
        fake = FakeDo()
        d = _daemon(fake)
        c = Counter(d)
        d.apps[1] = c
        with helpers.quiet():
            d._switch_page(1)                       # molette gauche
            pend = d._pending_activate              # ... puis le thread d'affichage :
            d._pending_activate = None
            d._entering = pend
            d._activate(pend)                       # le faux rejoue les callbacks
            d._enter_app(pend, force=True)
            d._entering = None
        self.assertEqual(c.enters, 1)
        self.assertEqual(d.page, 1)

    def test_firmware_buttons_enter_once_per_visit(self):
        fake = FakeDo()
        d = _daemon(fake)
        c = Counter(d)
        d.apps[1] = c
        with helpers.quiet():
            fake._cb(4242, fipd3.FW_BASE + 0, False)     # bouton page du firmware
            fake._cb(4242, fipd3.FW_BASE + 1, True)
        self.assertEqual(c.enters, 1)
        self.assertEqual(d.page, 1)

    def test_auto_heal_reenters_to_restore_leds(self):
        fake = FakeDo()
        d = _daemon(fake)
        c = Counter(d)
        d.apps[0] = c
        d._entered = c
        with helpers.quiet():
            d._entering = 0                       # sequence de _keepalive
            d._activate(0)                       # Remove+AddPage : tampons perdus
            d._enter_app(0, force=True)
            d._entering = None
        self.assertEqual(c.enters, 1)


class OverlayTests(unittest.TestCase):
    def test_image_activates_page_one_and_comes_back(self):
        """_send ecrivait sur la page 1 quelle que soit la page courante :
        E_PAGENOTACTIVE des qu'on n'etait pas sur la page 1."""
        fake = FakeDo()
        d = _daemon(fake)
        d.page = 3
        with helpers.quiet():
            d.show_image(Image.new('RGB', (320, 240), (200, 10, 10)))
            self.assertEqual(d.mode, 'image')
            self.assertEqual(d.page, 0)
            self.assertEqual(d._overlay_from, 3)
            self.assertEqual(fake.named('AddPage')[-1], ('AddPage', 1, 1))   # page 1 COURANTE
            self.assertEqual(fake.named('SetImage')[-1][1], 1)
            d._end_overlay()
        self.assertEqual((d.mode, d.page, d._pending_activate), ('ui', 3, 3))

    def test_failed_image_restores_the_ui(self):
        """Un /screen echoue laissait le daemon en mode image, sans image."""
        fake = FakeDo()
        d = _daemon(fake)
        d.page = 2
        fake.dead = True
        with helpers.quiet():
            with self.assertRaises(OSError):
                d.show_image(Image.new('RGB', (320, 240)))
        self.assertEqual(d.mode, 'ui')
        self.assertEqual(d.page, 2)


class VideoTests(unittest.TestCase):
    """play_video avec un cv2 factice : meme mecanique de page que /screen."""

    def _cv2(self, cap_factory):
        import types
        mod = types.ModuleType('cv2')
        mod.VideoCapture = lambda path: cap_factory()
        mod.COLOR_BGR2RGB = 0
        mod.cvtColor = lambda frame, code: frame
        return mod

    def _frame(self):
        try:
            import numpy
            return numpy.zeros((24, 32, 3), dtype=numpy.uint8)
        except ImportError:
            return None

    def _cap(self, frame, limit=None):
        state = {'n': 0}

        class Cap:
            def isOpened(self):
                return True

            def read(self):
                state['n'] += 1
                if limit is not None and state['n'] > limit:
                    return False, None
                return True, frame

            def release(self):
                pass

        return Cap(), state

    def setUp(self):
        self.frame = self._frame()
        if self.frame is None:
            self.skipTest('numpy absent : cv2 ne peut pas etre simule')

    def test_video_runs_on_page_one_then_comes_back(self):
        import sys
        cap, _ = self._cap(self.frame, limit=6)
        sys.modules['cv2'] = self._cv2(lambda: cap)
        try:
            fake = FakeDo()
            d = _daemon(fake)
            d.page = 2
            with helpers.quiet():
                r = d.play_video('clip.mp4', fps=400)
        finally:
            sys.modules.pop('cv2', None)
        self.assertEqual((r['frames'], r['stopped']), (6, False))
        self.assertEqual((d.mode, d.page, d._pending_activate), ('ui', 2, 2))
        self.assertEqual(fake.named('AddPage')[-1], ('AddPage', 1, 1))     # page 1 COURANTE
        self.assertEqual([c[1] for c in fake.named('SetImage')], [1] * 6)  # ecrite sur la page 1

    def test_s1_stops_the_video(self):
        import sys
        cap, _ = self._cap(self.frame)                  # flux ininterrompu
        sys.modules['cv2'] = self._cv2(lambda: cap)
        try:
            fake = FakeDo()
            d = _daemon(fake)
            out = []
            t = threading.Thread(target=lambda: out.append(d.play_video('clip.mp4', fps=400)))
            t.start()
            for _ in range(300):                        # quelques images parties...
                if len(fake.named('SetImage')) >= 3:
                    break
                time.sleep(0.01)
            d._on_btn(4242, 0x20, None)                 # ... puis appui S1
            t.join(5)
        finally:
            sys.modules.pop('cv2', None)
        self.assertFalse(t.is_alive())
        self.assertTrue(out[0]['stopped'])
        self.assertLess(out[0]['frames'], 400)
        self.assertEqual((d.mode, d.page), ('ui', 0))


class DisplayHealthTests(unittest.TestCase):
    def test_ok_age_follows_accepted_images(self):
        fake = FakeDo()
        d = _daemon(fake)
        self.assertIsNone(d.ok_age())
        with helpers.quiet():
            self.assertTrue(d._refresh_page(0))
            d._send(Image.new('RGB', (320, 240)))   # mode image : acceptee aussi
        age = d.ok_age()
        self.assertIsNotNone(age)
        self.assertLess(age, 5)
        fake.dead = True                           # le peripherique disparait
        with helpers.quiet():
            self.assertFalse(d._refresh_page(0))
        d._last_ok -= 30                           # ... rien n'est accepte depuis 30 s
        self.assertGreater(d.ok_age(), 25)


class RecoverTests(unittest.TestCase):
    """Reprise de session : mecanique, cadence, garde-fous."""

    def _d(self, **kw):
        fake = FakeDo()
        d = _daemon(fake)
        d._inited = True
        for k, v in kw.items():
            setattr(d, k, v)
        return fake, d

    def test_session_loss_triggers_reattach_once_per_delay(self):
        fake, d = self._d(_lost=True, _retry_at=0)
        with mock.patch.object(fipd3, 'service_state', return_value='RUNNING'), \
                mock.patch.object(d, '_init_stack', return_value=True) as init, \
                helpers.quiet():
            d._recover()
            d._recover()                           # trop tot : ignore
        self.assertEqual(init.call_count, 1)
        self.assertEqual(fake.named('Deinitialize'), [('Deinitialize',)])
        self.assertEqual(d.recoveries, 1)

    def test_absent_device_is_retried_without_deinitialize(self):
        fake, d = self._d(_lost=False, _retry_at=0)      # jamais eu de peripherique
        with mock.patch.object(fipd3, 'service_state', return_value='RUNNING'), \
                mock.patch.object(d, '_init_stack', return_value=False) as init, \
                helpers.quiet():
            d._recover()
        self.assertEqual(init.call_count, 1)
        self.assertEqual(fake.named('Deinitialize'), [])

    def test_reattach_waits_for_a_running_service(self):
        """Initialize attend son pipe INDEFINIMENT si le service ne tourne pas."""
        fake, d = self._d(_lost=True)
        with mock.patch.object(fipd3, 'service_state', return_value='STOPPED'), \
                helpers.quiet():
            self.assertFalse(d._reattach(full=True))
        self.assertEqual(fake.named('Initialize'), [])
        self.assertEqual(fake.named('Deinitialize'), [])
        self.assertIn('en attente', d.status)

    def test_stuck_recovery_stops_retrying(self):
        fake, d = self._d(_stuck=True, _retry_at=0)
        with mock.patch.object(d, '_init_stack', return_value=True) as init:
            d._recover()
        self.assertEqual(init.call_count, 0)

    def test_recover_retries_quickly_then_every_ten_seconds(self):
        """Apres une perte le service revient en quelques secondes : on ne
        l'attend pas 10 s pour chaque essai."""
        fake, d = self._d(_lost=True, _retry_at=0, _lost_since=time.monotonic())
        with mock.patch.object(fipd3, 'service_state', return_value='STOPPED'), \
                helpers.quiet():
            before = time.monotonic()
            d._recover()
            self.assertAlmostEqual(d._retry_at - before, fipd3.RECOVER_QUICK_S, delta=0.3)
            d._retry_at = 0
            d._lost_since = time.monotonic() - fipd3.RECOVER_QUICK_FOR_S - 1
            before = time.monotonic()
            d._recover()
            self.assertAlmostEqual(d._retry_at - before, fipd3.RECOVER_EVERY_S, delta=0.3)


class DeviceSelectionTests(unittest.TestCase):
    """Constate sur le materiel (23/11) : apres un redemarrage du service,
    Enumerate retourne l'ANCIEN peripherique — un handle mort (E_HANDLE a
    chaque appel) — EN PLUS du nouveau."""

    def _daemon(self, fake):
        d = fipd3.Daemon(with_apps=False)
        d.do = fake
        fake.daemon = d
        return d

    def _init(self, d):
        with helpers.quiet(), mock.patch('fipd3.time.sleep'):
            return d._init_stack()

    def test_a_dead_handle_listed_first_is_skipped(self):
        fake = FakeDo()
        fake.devices, fake.dead_handles = [1111, 2222], {1111}
        d = self._daemon(fake)
        self.assertTrue(self._init(d))
        self.assertEqual(d.hdev, 2222)
        self.assertIn('1 handle(s) mort(s)', d.status)

    def test_only_dead_handles_is_a_failure_never_a_false_success(self):
        fake = FakeDo()
        fake.devices, fake.dead_handles = [1111, 2222], {1111, 2222}
        d = self._daemon(fake)
        d._inited = True
        with mock.patch.object(fipd3, 'service_state', return_value='RUNNING'),                 helpers.quiet():
            self.assertFalse(d._reattach(full=True))
        self.assertEqual(d.recoveries, 0)        # pas de « session retablie »
        self.assertIsNone(d.hdev)
        self.assertTrue(d.status.startswith('peripherique muet'))

    def test_page_shown_becomes_the_active_one_at_reinit(self):
        """Pas de flash d'horloge a la reprise : la page affichee redevient
        courante et son image est la premiere acceptee."""
        fake = FakeDo()
        d = self._daemon(fake)
        self.assertTrue(self._init(d))
        d.page = 3
        fake.calls.clear()
        self.assertTrue(self._init(d))
        # 6 inscriptions (la page 4 ACTIVE) puis la re-activation de la page 4
        self.assertEqual(fake.named('AddPage'),
                         [('AddPage', fipd3.FW_BASE + i, 1 if i == 3 else 0)
                          for i in range(6)] + [('AddPage', fipd3.FW_BASE + 3, 1)])
        self.assertEqual(fake.named('SetImage')[-1][1], fipd3.FW_BASE + 3)

    def test_initialize_failure_is_reported_not_raised(self):
        """Juste apres un redemarrage du service, Initialize rend E_FAIL."""
        fake = FakeDo()
        fake.init_failures = 1
        d = self._daemon(fake)
        self.assertFalse(self._init(d))
        self.assertIn('Initialize hr=0x80004005', d.status)
        self.assertTrue(self._init(d))


class KeepaliveRecoveryTests(unittest.TestCase):
    """La boucle d'affichage REELLE face aux pannes, DirectOutput simulee."""

    def _run(self, d, fake, cond, limit=10.0):
        """Lance la vraie _keepalive (cadences raccourcies) jusqu'a cond()."""
        real_sleep = time.sleep
        out = []
        with mock.patch('fipd3.time.sleep'), \
                mock.patch.object(fipd3, 'print', lambda *a, **k: out.append(str(a[0]) if a else ''),
                                  create=True), \
                mock.patch.object(fipd3, 'FAIL_WAIT_S', 0.02), \
                mock.patch.object(fipd3, 'KEEPALIVE_S', 0.02), \
                mock.patch.object(fipd3, 'RECOVER_QUICK_S', 0.02), \
                mock.patch.object(fipd3, 'service_state', return_value='RUNNING'):
            d._ka_thread = threading.Thread(target=d._keepalive, daemon=True)
            d._ka_thread.start()
            t0 = time.time()
            while time.time() - t0 < limit and not cond():
                real_sleep(0.02)
            real_sleep(0.3)                       # laisse la boucle se stabiliser
            deinit = len(fake.named('Deinitialize'))
            # etat AVANT l'arret : shutdown() vide le handle, et un rafraichissement
            # en cours compterait alors un echec sans consequence
            hdev, counters = d.hdev, (d._fails, d._dead)
            d.shutdown()
            d._ka_thread.join(5)
        self.assertFalse(d._ka_thread.is_alive())
        return out, deinit, hdev, counters

    def test_firmware_quirk_heals_then_rebuilds_if_it_persists(self):
        """Page jamais active (E_PAGENOTACTIVE) : on tente l'auto-guerison,
        puis on reconstruit la session entiere si rien n'y fait."""
        fake = FakeDo()
        d = _daemon(fake)
        d._inited = True
        fake.dead = True
        fake.on_initialize = lambda: setattr(fake, 'dead', False)   # le firmware revient
        out, deinit, _, counters = self._run(d, fake, lambda: d.recoveries > 0)
        self.assertEqual(d.recoveries, 1)
        self.assertTrue(any('[auto-guerison]' in line for line in out))   # on a soigne...
        self.assertEqual(deinit, 1)                                       # ...puis reconstruit
        self.assertTrue(fake.named('SetImage'))                           # images acceptees
        self.assertEqual(fake.named('AddPage')[-1][1:], (fipd3.FW_BASE + d.page, 1))
        self.assertEqual(counters, (0, 0))

    def test_daemon_survives_a_service_restart(self):
        """Le scenario REEL du 23/11 : le service Logitech redemarre. Pendant
        l'arret -> E_UNEXPECTED ; a la reprise Initialize rend E_FAIL une fois
        (service pas encore pret) ; puis Enumerate donne l'ANCIEN handle (mort,
        E_HANDLE) ET le nouveau. Avant la correction le daemon prenait le
        handle mort et se declarait retabli a jamais."""
        fake = FakeDo()
        d = _daemon(fake)
        d._inited = True
        d.page = 4
        fake.service_down = True                 # le service s'arrete...
        real_sleep = time.sleep

        def service_back():                      # ... et revient peu apres
            real_sleep(0.3)
            fake.init_failures = 1               # reparti mais pas encore pret
            fake.on_initialize = lambda: (
                fake.dead_handles.add(4242), setattr(fake, 'devices', [4242, 5353]))
            fake.service_down = False

        threading.Thread(target=service_back, daemon=True).start()
        out, deinit, hdev, counters = self._run(
            d, fake, lambda: d.recoveries > 0 and d.ok_age() is not None and d.ok_age() < 5)
        self.assertEqual(d.recoveries, 1)               # ni zero (jamais retabli)...
        self.assertEqual(hdev, 5353)                    # ...ni le handle mort
        self.assertTrue(any('erreur de session' in line for line in out))
        self.assertFalse(any('[auto-guerison]' in line for line in out))   # inutile ici
        self.assertEqual(deinit, 1)
        self.assertGreaterEqual(len(fake.named('Initialize')), 3)   # arrete, E_FAIL, pret
        self.assertEqual(fake.named('AddPage')[-1][1:], (fipd3.FW_BASE + 4, 1))
        self.assertEqual(fake.named('SetImage')[-1][1], fipd3.FW_BASE + 4)
        self.assertEqual((d.page, d.mode), (4, 'ui'))
        self.assertEqual(counters, (0, 0))


class _HttpCase(unittest.TestCase):
    def setUp(self):
        # JAMAIS le vrai fipd_bindings.json : POST /bindings l'ecrirait.
        self.tmp = tempfile.mkdtemp(prefix='fipd_cfg_')
        self._bf = fipd3.BINDINGS_FILE
        fipd3.BINDINGS_FILE = os.path.join(self.tmp, 'fipd_bindings.json')
        if os.path.exists(self._bf):
            shutil.copy(self._bf, fipd3.BINDINGS_FILE)
        self.fake = FakeDo()
        self.d = _daemon(self.fake)
        self.d.page = 2
        self._old = fipd3.D
        fipd3.D = self.d
        self.srv = fipd3.Server(('127.0.0.1', 0), fipd3.H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        fipd3.D = self._old
        fipd3.BINDINGS_FILE = self._bf
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _req(self, path, method='GET', data=None, headers=None):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path),
                                     data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode('utf-8'))
            e.close()
            return e.code, body


class HttpGuardTests(_HttpCase):
    def test_browser_origin_is_refused(self):
        """Une page web ne doit pas pouvoir piloter l'API (/bindings = touches)."""
        code, body = self._req('/keys', headers={'Origin': 'https://exemple.invalid'})
        self.assertEqual((code, body['error']), (403, 'origine refusee'))
        code, _ = self._req('/keys', headers={'Origin': 'null'})
        self.assertEqual(code, 403)
        code, _ = self._req('/apps', method='POST', data=b'{}',
                           headers={'Origin': 'https://exemple.invalid'})
        self.assertEqual(code, 403)

    def test_foreign_host_is_refused(self):
        code, _ = self._req('/keys', headers={'Host': 'attaque.example:8199'})
        self.assertEqual(code, 403)

    def test_local_requests_pass(self):
        code, body = self._req('/keys')
        self.assertEqual(code, 200)
        self.assertIn('win', body['keys'])
        code, _ = self._req('/apps', headers={'Host': 'localhost:%d' % self.port})
        self.assertEqual(code, 200)

    def test_apps_reports_display_health(self):
        """/apps expose l'age de la derniere image acceptee et le nombre de
        recuperations : c'est ce que lit le test materiel de reprise."""
        code, body = self._req('/apps')
        self.assertEqual(code, 200)
        self.assertIn('ok_age', body)
        self.assertIn('recoveries', body)
        self.assertIsNone(body['ok_age'])          # rien encore envoye (faux peripherique)

    def test_screen_url_must_be_http(self):
        for url in ('file:///C:/Windows/win.ini', 'ftp://x/y', 'gopher://x'):
            code, _ = self._req('/screen?url=' + urllib.parse.quote(url),
                                method='POST', data=b'')
            self.assertEqual(code, 400, url)


class HttpApiTests(_HttpCase):
    def test_screen_from_another_page_then_back(self):
        import io
        buf = io.BytesIO()
        Image.new('RGB', (64, 48), (0, 200, 0)).save(buf, 'PNG')
        code, body = self._req('/screen', method='POST', data=buf.getvalue())
        self.assertEqual((code, body['ok'], body['mode'], body['page']), (200, True, 'image', 2))
        self.assertEqual(self.fake.named('SetImage')[-1][1], 1)
        code, body = self._req('/ui', method='POST', data=b'')
        self.assertEqual((code, body['mode'], body['page']), (200, 'ui', 2))

    def test_bindings_are_validated(self):
        bad = [b'[]', b'{"bindings": [], "pages": {}}', b'{"bindings": {"S1": "nope"}}',
               b'{"bindings": {"XX": "f5"}}', b'{"bindings": {"S1": 3}}']
        for raw in bad:
            code, body = self._req('/bindings', method='POST', data=raw)
            self.assertEqual(code, 400, raw)
        code, body = self._req('/bindings', method='POST',
                               data=json.dumps({'paging': False,
                                                'bindings': {'S1': 'ctrl+shift+s'},
                                                'pages': {'2': {'S2': 'f5'}}}).encode())
        self.assertEqual(code, 200)
        self.assertEqual(body['bindings']['bindings']['S1'], 'ctrl+shift+s')
        code, _ = self._req('/bindings')
        self.assertEqual(code, 200)


class ReloadTests(unittest.TestCase):
    APP_A = ("import fipui\n"
             "import charte\n"
             "class A(fipui.App):\n"
             "    name = 'appa'\n"
             "    title = 'A'\n"
             "    tag = %d\n")
    APP_B = ("import fipui\n"
             "class B(fipui.App):\n"
             "    name = 'appb'\n"
             "    title = 'B'\n")
    APP_C = ("import fipui\n"
             "import fipshared_sh\n"            # module partage factice (fipd_shared_*)
             "class C(fipui.App):\n"
             "    name = 'appc'\n"
             "    title = 'C'\n"
             "    tag = fipshared_sh.tag\n")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='fipd_apps_')
        self._dir = fipd3.APPS_DIR
        fipd3.APPS_DIR = self.dir
        self._write('a.py', self.APP_A % 1)
        self._write('b.py', self.APP_B)

    def tearDown(self):
        fipd3.APPS_DIR = self._dir
        for name in list(sys.modules):
            if name.startswith('_fipapp_'):
                sys.modules.pop(name, None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, text):
        # Sans attente : un fichier modifie se detecte par son contenu. Sur sa
        # date, deux ecritures rapprochees peuvent en avoir la meme.
        with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as f:
            f.write(text)

    def _load(self):
        fake = FakeDo()
        d = fipd3.Daemon()
        d.do, d.hdev = fake, 4242
        fake.daemon = d
        d._entered = d.apps[0]
        return d, fake

    def test_same_size_edit_right_after_loading_is_rebuilt(self):
        """L'echec de la premiere CI : 'tag = 1' -> 'tag = 2' (meme longueur),
        enregistre aussitot apres le chargement. Le daemon annoncait le
        rechargement et relançait l'ancien code. Le .pyc de l'ancien est arme
        pour passer pour a jour : Python n'y voit que la meme date (a la
        seconde) et la meme taille. La source doit l'emporter."""
        with helpers.quiet():
            d, fake = self._load()
            a = d.apps[6]
            src = os.path.join(self.dir, 'a.py')
            pyc = py_compile.compile(src, doraise=True)   # bytecode de 'tag = 1'
            self._write('a.py', self.APP_A % 2)
            _pass_as_current(pyc, src)
            r = d.reload_apps()
            self.assertEqual(r['reloaded'], ['a.py'])
            self.assertIsNot(d.apps[6], a)
            self.assertEqual(d.apps[6].tag, 2)

    @staticmethod
    def _registered(fake):
        return len(fake.named('RemovePage')) + len(fake.named('AddPage'))

    def test_only_changed_files_are_rebuilt(self):
        with helpers.quiet():
            d, fake = self._load()
            a, b = d.apps[6], d.apps[7]
            self.assertEqual((a.name, b.name, a.tag), ('appa', 'appb', 1))
            n_dev = self._registered(fake)
            self._write('a.py', self.APP_A % 2)
            r = d.reload_apps()
            self.assertEqual(r['reloaded'], ['a.py'])
            self.assertIs(d.apps[7], b)                   # B garde son instance (etat)
            self.assertIsNot(d.apps[6], a)
            self.assertEqual(d.apps[6].tag, 2)
            self.assertEqual(self._registered(fake), n_dev)   # memes pages : rien a re-enregistrer
            self.assertTrue(a._stop.is_set())             # ancienne instance stoppee

    def test_nothing_changed_keeps_everything(self):
        with helpers.quiet():
            d, fake = self._load()
            a, b = d.apps[6], d.apps[7]
            r = d.reload_apps()
            self.assertEqual((r['reloaded'], r['kept']), ([], ['appa', 'appb']))
            self.assertIs(d.apps[6], a)
            self.assertIs(d.apps[7], b)

    def test_broken_file_keeps_the_working_version(self):
        with helpers.quiet():
            d, fake = self._load()
            a = d.apps[6]
            self._write('a.py', 'def broken(:\n')
            r = d.reload_apps()
            self.assertTrue(r['errors'])
            self.assertIs(d.apps[6], a)                   # l'ancienne reste en service
            self.assertEqual(d._npages(), 8)

    def test_forced_reload_and_new_file(self):
        with helpers.quiet():
            d, fake = self._load()
            b = d.apps[7]
            r = d.reload_apps('appb')
            self.assertEqual(r['reloaded'], ['b.py'])
            self.assertIsNot(d.apps[7], b)
            self._write('c.py', "import fipui\n"
                               "class C(fipui.App):\n    name = 'appc'\n    title = 'C'\n")
            r = d.reload_apps()
            self.assertEqual(r['reloaded'], ['c.py'])
            self.assertEqual([a.name for a in d.apps[6:]], ['appa', 'appb', 'appc'])
            self.assertTrue(fake.named('RemovePage'))      # pages re-enregistrees

    def test_changed_shared_module_rebuilds_all(self):
        with helpers.quiet():
            d, fake = self._load()
            a, b = d.apps[6], d.apps[7]
            d._shared['charte'] = None                    # simule charte.py modifie
            r = d.reload_apps()
            self.assertEqual(sorted(r['reloaded']), ['a.py', 'b.py'])
            self.assertIsNot(d.apps[6], a)
            self.assertIsNot(d.apps[7], b)

    def test_changed_shared_module_is_compiled_from_source(self):
        """Module partage modifie : les apps repartent avec le NOUVEAU code.
        Le .pyc de l'ancien est arme pour passer pour a jour (date a la seconde
        et taille de la source) : un import ordinaire le reprendrait."""
        shared = tempfile.mkdtemp(prefix='fipd_shared_')
        self._write('a.py', self.APP_C)
        try:
            sh = os.path.join(shared, 'sh.py')
            _put(sh, 'tag = 1\n')
            fipd3._module_from_source('fipshared_sh', sh)
            pyc = py_compile.compile(sh, doraise=True)     # bytecode de l'ancien code
            with helpers.quiet(), mock.patch.object(fipd3, 'HERE', shared):
                d, fake = self._load()
                self.assertEqual((d.apps[6].tag, d._shared), (1, {'fipshared_sh': fipd3._stat(sh)}))
                _put(sh, 'tag = 2\n')                      # meme longueur, aussitot
                _pass_as_current(pyc, sh)
                r = d.reload_apps()
                self.assertEqual((r['errors'], sorted(r['reloaded'])), ([], ['a.py', 'b.py']))
                self.assertEqual(d.apps[6].tag, 2)
                self.assertEqual(sys.modules['fipshared_sh'].tag, 2)
        finally:
            sys.modules.pop('fipshared_sh', None)
            shutil.rmtree(shared, ignore_errors=True)

    def test_broken_shared_module_keeps_the_old_one(self):
        shared = tempfile.mkdtemp(prefix='fipd_shared_')
        self._write('a.py', self.APP_C)
        try:
            sh = os.path.join(shared, 'sh.py')
            _put(sh, 'tag = 1\n')
            old = fipd3._module_from_source('fipshared_sh', sh)
            with helpers.quiet(), mock.patch.object(fipd3, 'HERE', shared):
                d, fake = self._load()
                a = d.apps[6]
                _put(sh, 'def broken(:\n')
                r = d.reload_apps()
                self.assertTrue(r['errors'])
                self.assertEqual((r['reloaded'], r['restart_required']), ([], []))
                self.assertIs(sys.modules['fipshared_sh'], old)   # l'ancien reste en service
                self.assertIs(d.apps[6], a)
        finally:
            sys.modules.pop('fipshared_sh', None)
            shutil.rmtree(shared, ignore_errors=True)

    def test_restart_required_is_reported(self):
        with helpers.quiet():
            d, fake = self._load()
            d._shared['uiatabs'] = None
            r = d.reload_apps()
            self.assertEqual(r['restart_required'], ['uiatabs'])
            self.assertEqual(r['reloaded'], [])


class FingerprintTests(unittest.TestCase):
    """fipd3._stat : empreinte du CONTENU, base de la recharge a chaud ciblee
    (apps tierces, modules partages, daemon)."""
    FILE = 'fipd_fingerprint_test.py'

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='fipd_fp_')
        self.path = os.path.join(self.dir, self.FILE)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, text):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text)
        return self.path

    def test_missing_file(self):
        self.assertIsNone(fipd3._stat(self.path))

    def test_same_content_same_fingerprint(self):
        self._write('tag = 1\n')
        first = fipd3._stat(self.path)
        self._write('tag = 1\n')                        # enregistre a l'identique
        self.assertEqual(fipd3._stat(self.path), first)   # rien a reconstruire

    def test_same_size_edit_in_the_same_clock_tick(self):
        """Sur la date, deux enregistrements de meme longueur dans le meme top
        d'horloge sont indiscernables : meme date ET meme taille. La date est
        figee ici pour reproduire la situation sur toute machine."""
        self._write('tag = 1\n')
        first = fipd3._stat(self.path)
        v1 = os.stat(self.path)
        self._write('tag = 2\n')                        # aussitot : meme longueur
        with mock.patch.object(fipd3.os, 'stat', return_value=v1):
            self.assertEqual(os.stat(self.path).st_size, v1.st_size)
            self.assertEqual(os.stat(self.path).st_mtime_ns, v1.st_mtime_ns)
            self.assertNotEqual(fipd3._stat(self.path), first)


class SourceLoadTests(unittest.TestCase):
    """fipd3._module_from_source : apps tierces et modules partages sont
    compiles depuis la source a chaque chargement. Python reprendrait sinon un
    .pyc perime des que la source garde la meme taille dans la meme seconde
    (l'echec CI : le daemon annoncait le rechargement et relançait l'ancien
    code)."""
    NAME = '_fipd_sourceload_test'

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='fipd_src_')
        self.path = os.path.join(self.dir, 'mod.py')

    def tearDown(self):
        sys.modules.pop(self.NAME, None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, text):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_stale_pyc_is_never_used(self):
        self._write('tag = 1\n')
        pyc = py_compile.compile(self.path, doraise=True)   # bytecode de l'ancien code
        self._write('tag = 2\n')                            # meme longueur
        _pass_as_current(pyc, self.path)
        spec = importlib.util.spec_from_file_location(self.NAME + '_plain', self.path)
        plain = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plain)                      # import ORDINAIRE :
        if plain.tag != 1:                                  # il croit le .pyc a jour
            self.skipTest('cette version de Python ne reprend plus un .pyc perime')
        mod = fipd3._module_from_source(self.NAME, self.path)
        self.assertEqual(mod.tag, 2)
        self.assertIs(sys.modules[self.NAME], mod)
        self.assertEqual(mod.__file__, self.path)

    def test_no_pyc_is_written(self):
        self._write('tag = 1\n')
        fipd3._module_from_source(self.NAME, self.path)
        self.assertEqual(os.listdir(self.dir), ['mod.py'])

    def test_failed_run_keeps_the_previous_module(self):
        self._write('tag = 1\n')
        old = fipd3._module_from_source(self.NAME, self.path)
        self._write('def broken(:\n')
        with self.assertRaises(SyntaxError):
            fipd3._module_from_source(self.NAME, self.path)
        self.assertIs(sys.modules[self.NAME], old)

    def test_failed_first_run_leaves_no_module(self):
        self._write('def broken(:\n')
        with self.assertRaises(SyntaxError):
            fipd3._module_from_source(self.NAME, self.path)
        self.assertNotIn(self.NAME, sys.modules)


if __name__ == '__main__':
    unittest.main()
