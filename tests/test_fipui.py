"""fipui : alertes (ajustees a l'ecran, en attente si page masquee), jauges,
pollers lies au cycle de vie de la page."""
import threading
import time
import unittest

import helpers
import fipui
from helpers import FakeDaemon

from PIL import Image, ImageDraw


def _blank():
    img = Image.new('RGB', (320, 240), fipui.BG)
    return img, ImageDraw.Draw(img)


def _text_extent(img):
    """(x_min, x_max, y_min, y_max) des pixels de TEXTE du bandeau (ou None).
    Le texte est antialiase : on ne cherche pas du blanc pur, mais ce qui
    n'est ni le fond ni le rouge du bandeau."""
    px = img.load()
    pts = [(x, y) for y in range(27) for x in range(320)
           if px[x, y][:3] not in (fipui.BG, fipui.RED)]
    if not pts:
        return None
    return (min(p[0] for p in pts), max(p[0] for p in pts),
            min(p[1] for p in pts), max(p[1] for p in pts))


def _bare_app(d=None):
    """Une App sans __init__ complet : juste les attributs des alertes."""
    app = fipui.App.__new__(fipui.App)
    app.d = d or FakeDaemon()
    app.leds = {}
    app._alert = None
    app._shown = threading.Event()
    app._stop = threading.Event()
    app._polls = []
    return app


class AlertTests(unittest.TestCase):
    def test_banner_never_wider_than_screen(self):
        """29 caracteres debordaient (363 px pour 320) : la police doit ceder."""
        for msg in ('Operation terminee avec succes', 'x' * 300):
            app = _bare_app()
            app.alert(msg)
            img, d = _blank()
            app.draw_alert(d, fipui.font(20))
            ext = _text_extent(img)
            self.assertIsNotNone(ext, msg)
            self.assertGreaterEqual(ext[0], 4, msg)
            self.assertLessEqual(ext[1], 315, msg)

    def test_short_alert_keeps_big_font(self):
        """Un message court garde la police d'origine (20 px, pas 14)."""
        app = _bare_app()
        app.alert('OK')
        img, d = _blank()
        app.draw_alert(d, fipui.font(20))
        height = _text_extent(img)[3] - _text_extent(img)[2] + 1
        small = Image.new('RGB', (320, 27), fipui.BG)
        ImageDraw.Draw(small).text((10, 13), 'OK', font=fipui.font(14), fill=(255, 255, 255),
                                   anchor='lm')
        self.assertGreater(height, _text_extent(small)[3] - _text_extent(small)[2] + 1)

    def test_pending_alert_waits_for_display(self):
        """Une alerte lancee page masquee n'est pas perdue : elle demarre a l'affichage."""
        d = FakeDaemon([object()])           # une AUTRE page est affichee
        app = _bare_app(d)
        app.alert('CI en echec')
        self.assertTrue(app.alert_active())
        self.assertIsNone(app._alert[1])     # chrono non demarre
        time.sleep(0.05)
        self.assertTrue(app.alert_active())
        self.assertEqual(d.redraws, 0)       # pas de redraw pour une page masquee
        img, dr = _blank()
        app.draw_alert(dr, fipui.font(20))   # premiere fois qu'on la montre
        self.assertIsNotNone(app._alert[1])
        app._alert[1] = time.time() - fipui.ALERT_SECONDS - 1
        app.draw_alert(dr, fipui.font(20))
        self.assertFalse(app.alert_active())

    def test_alert_on_current_page_triggers_redraw(self):
        app = _bare_app()
        app.d.apps = [app]
        app.alert('coucou')
        self.assertEqual(app.d.redraws, 1)


class BarTests(unittest.TestCase):
    def test_zero_percent_draws_frame_only(self):
        img, d = _blank()
        fipui.draw_bar(d, 15, 75, 305, 87, 0.0, (80, 200, 120))
        inside = [img.getpixel((x, 81)) for x in range(16, 304)]
        self.assertNotIn((80, 200, 120), inside)

    def test_full_bar_reaches_right_edge(self):
        img, d = _blank()
        fipui.draw_bar(d, 15, 75, 305, 87, 1.0, (80, 200, 120))
        self.assertEqual(img.getpixel((303, 81)), (80, 200, 120))


class PollLifecycleTests(unittest.TestCase):
    def test_sleeps_when_hidden_runs_when_shown(self):
        calls = []
        hits = []

        class P(fipui.App):
            name = 'polltest'

            def __init__(self):
                super().__init__(FakeDaemon())
                self.thread = self.poll(
                    lambda: (calls.append(1), hits.append(threading.current_thread().name)),
                    0.05)

        app = P()
        time.sleep(0.3)
        self.assertEqual(calls, [])          # page masquee : rien ne tourne
        app._shown.set()
        app._wake_polls()
        time.sleep(0.3)
        self.assertGreater(len(calls), 2)    # passe immediate, puis cadence 50 ms
        self.assertEqual(set(hits), {'polltest-poll'})
        app._shown.clear()
        app._wake_polls()
        time.sleep(0.3)                      # le temps d'un dernier intervalle
        n = len(calls)
        time.sleep(0.4)
        self.assertLessEqual(len(calls) - n, 1)   # puis plus rien
        n2 = len(calls)
        time.sleep(0.3)
        self.assertEqual(len(calls), n2)
        app._stop.set()
        # page masquee : le thread endormi ne voit _stop qu'a la fin de son
        # attente (0,5 s max) -> on l'attend, sans miser sur une duree de sommeil
        app.thread.join(2)
        self.assertFalse(app.thread.is_alive())

    def test_poll_survives_exception(self):
        calls = []
        boom = {'on': True}

        def fn():
            calls.append(1)
            if boom['on']:
                raise RuntimeError('panne du poll')

        class P(fipui.App):
            name = 'polltest2'

            def __init__(self):
                super().__init__(FakeDaemon())
                self.poll(fn, 0.05)

        app = P()
        app._shown.set()
        app._wake_polls()
        with helpers.quiet():
            time.sleep(0.4)
            boom['on'] = False
            n = len(calls)
            time.sleep(0.4)
        self.assertGreater(len(calls) - n, 2)   # le thread a survecu a l'exception
        app._stop.set()


if __name__ == '__main__':
    unittest.main()
