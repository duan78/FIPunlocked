"""FENETRES : confirmations attachees a leur cible, touches volume des onglets."""
import time
import unittest
from unittest import mock

import helpers
import fenetres
import keysend


def _app(wins=(), tabs=()):
    d = helpers.FakeDaemon()
    with helpers.no_threads():
        app = fenetres.FenetresApp(d)
    app.wins = list(wins)
    app.tabs = list(tabs)
    app.tabs_t = time.time() if tabs else 0.0
    app.cursor = 0
    return app


def _win(hwnd, pid, proc='app.exe', title='Fenetre'):
    return {'kind': 'win', 'hwnd': hwnd, 'pid': pid, 'proc': proc, 'title': title,
            'min': False, 'top': False, 'fg': True}


class ConfirmTests(unittest.TestCase):
    def test_second_press_must_target_the_same_window(self):
        posted = []
        app = _app(wins=[_win(101, 1001, title='A'), _win(102, 1002, title='B')])
        with mock.patch.object(fenetres.u32, 'PostMessageW',
                               lambda *a: posted.append(a) or 1):
            app.on_event('S4', True)          # arme 'fermer' sur la fenetre A
            app.on_event('right_cw', True)    # le curseur passe sur B
            app.on_event('S4', True)          # B != cible armee : on ne ferme RIEN
            self.assertEqual(posted, [])
            app.on_event('S4', True)          # 2e appui sur B : ferme B
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], 102)

    def test_kill_is_bound_to_the_pid(self):
        killed = []
        import psutil

        class P:
            def __init__(self, pid):
                self.pid = pid

            def kill(self):
                killed.append(self.pid)

        app = _app(wins=[_win(201, 2001), _win(202, 2002)])
        with mock.patch.object(psutil, 'Process', P):
            app.on_event('S5', True)
            app.on_event('right_cw', True)
            app.on_event('S5', True)
            self.assertEqual(killed, [])
            app.on_event('S5', True)
        self.assertEqual(killed, [2002])

    def test_timeout_expires(self):
        app = _app(wins=[_win(301, 3001)])
        app.on_event('S4', True)
        app._armed = (app._armed[0], app._armed[1], time.time() - 4)
        app.on_event('right_cw', True)
        app.on_event('S4', True)
        self.assertIsNotNone(app._armed)        # re-arme, pas de confirmation


class TabActionTests(unittest.TestCase):
    TABS = [{'kind': 'tab', 'title': 'T1', 'active': True, 'url': 'https://a.b/', 'hwnd': 1},
            {'kind': 'tab', 'title': 'T2', 'active': False, 'url': '', 'hwnd': 1}]

    def test_volume_keys_actually_work_on_tabs(self):
        """Les keycaps Vol-/Vol+ etaient affiches sur un onglet sans effet."""
        app = _app(tabs=self.TABS)
        taps = []
        with mock.patch.object(keysend, 'tap', lambda k: taps.append(k)):
            app.on_event('S5', True)
            app.on_event('S6', True)
        self.assertEqual(taps, ['volumedown', 'volumeup'])

    def test_copy_reports_a_locked_clipboard(self):
        app = _app(tabs=self.TABS)
        with mock.patch.object(fenetres.fipui, 'clipboard_set', return_value=False):
            app.on_event('S2', True)
        self.assertEqual(app._alert[0], 'presse-papiers occupe')

    def test_list_renders_with_windows_and_tabs(self):
        from PIL import Image, ImageDraw
        import fipui
        app = _app(wins=[_win(i, 1000 + i, title='Fenetre %d' % i) for i in range(5)],
                   tabs=self.TABS)
        img = Image.new('RGB', (320, 240), fipui.BG)
        app.render(ImageDraw.Draw(img))
        self.assertEqual(img.size, (320, 240))


if __name__ == '__main__':
    unittest.main()
