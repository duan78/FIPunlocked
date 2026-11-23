"""TACHES : instantane systeme unique, kill lie au pid (pas a la ligne)."""
import os
import time
import unittest

import helpers
import psutil

import taskmgr


class SnapshotTests(unittest.TestCase):
    def test_snapshot_matches_psutil(self):
        snap = taskmgr._system_snapshot()
        if snap is None:
            self.skipTest('instantane systeme indisponible sur cette architecture')
        me = os.getpid()
        self.assertIn(me, snap)
        p = psutil.Process(me)
        t, m, io = p.cpu_times(), p.memory_info(), p.io_counters()
        self.assertEqual(snap[me]['name'].lower(), p.name().lower())
        self.assertAlmostEqual(snap[me]['cpu'] / 1e7, t.user + t.system, delta=0.5)
        self.assertAlmostEqual(snap[me]['rss'] / m.rss, 1.0, delta=0.2)
        # 25 % : les compteurs du processus test avancent entre les deux lectures
        self.assertAlmostEqual(snap[me]['io'] / (io.read_bytes + io.write_bytes), 1.0, delta=0.25)
        self.assertGreater(len(snap), 8)

    def test_one_pass_is_cheap(self):
        """Une passe coutait 5,3 s de CPU (3 instantanes psutil par processus)."""
        d = helpers.FakeDaemon()
        with helpers.no_threads():
            app = taskmgr.TaskMgrApp(d)
        c0, w0 = time.process_time(), time.perf_counter()
        app._measure()
        app._measure()
        cpu, wall = time.process_time() - c0, time.perf_counter() - w0
        self.assertLess(cpu, 1.0)
        self.assertLess(wall, 1.5)
        self.assertLessEqual(len(app.procs), 6)
        for r in app.procs:
            self.assertGreaterEqual(r['pid'], 500)


class FakeProc:
    def __init__(self, killed, pid):
        self.killed, self.pid = killed, pid

    def kill(self):
        self.killed.append(self.pid)


class KillTests(unittest.TestCase):
    def _app(self):
        d = helpers.FakeDaemon()
        with helpers.no_threads():
            app = taskmgr.TaskMgrApp(d)
        app.procs = [{'pid': 1000, 'name': 'a.exe', 'cpu': 1, 'mem': 1, 'io': 0},
                     {'pid': 1001, 'name': 'b.exe', 'cpu': 1, 'mem': 1, 'io': 0},
                     {'pid': 1002, 'name': 'c.exe', 'cpu': 1, 'mem': 1, 'io': 0}]
        return app

    def test_confirmation_follows_the_process_not_the_row(self):
        """La liste est retrieved entre les deux appuis : jamais un mauvais kill.
        Le 2e appui ne tue que si la ligne montree est encore celle du processus
        arme (cadre rouge) ; sinon il re-arme, sans tuer."""
        app = self._app()
        killed = []
        orig = psutil.Process
        psutil.Process = lambda pid: FakeProc(killed, pid)
        try:
            app.on_event('S2', True)              # arme b.exe (pid 1001), ligne 2
            self.assertEqual(app._armed[0], 1001)
            app.procs[1], app.procs[2] = app.procs[2], app.procs[1]   # retri
            app.on_event('S2', True)              # ligne 2 = c.exe desormais :
            self.assertEqual(killed, [])          #   on ne tue PAS c.exe
            self.assertEqual(app._armed[0], 1002)  #   on re-arme ce qui est affiche
            app.on_event('S3', True)              # ligne 3 = b.exe : re-arme b
            app.on_event('S3', True)              # confirme : b.exe meurt
        finally:
            psutil.Process = orig
        self.assertEqual(killed, [1001])

    def test_timeout_rearms(self):
        app = self._app()
        app.on_event('S1', True)
        app._armed = (app._armed[0], time.time() - 4)
        self.assertIsNone(app._armed_pid())

    def test_protected_process_is_never_armed(self):
        app = self._app()
        app.procs[0]['name'] = 'explorer.exe'
        killed = []
        orig = psutil.Process
        psutil.Process = lambda pid: FakeProc(killed, pid)
        try:
            app.on_event('S1', True)
            app.on_event('S1', True)
        finally:
            psutil.Process = orig
        self.assertEqual(killed, [])
        self.assertIsNone(app._armed)
        self.assertTrue(app._alert[0].startswith('protege'))

    def test_render_has_no_footer_over_last_row(self):
        """L'ancien pied de page ('S = tuer (2 appuis) - molette = tri') s'imprimait
        sur la 6e ligne : sa couleur ne doit plus apparaitre nulle part."""
        from PIL import Image, ImageDraw
        import fipui
        app = self._app()
        app.procs = [{'pid': 5000 + i, 'name': 'proc%d.exe' % i, 'cpu': 5.0, 'mem': 2.0,
                      'io': 0.0} for i in range(6)]
        img = Image.new('RGB', (320, 240), fipui.BG)
        app.render(ImageDraw.Draw(img))
        px = img.load()
        footer = (120, 120, 130)
        self.assertFalse(any(px[x, y] == footer for x in range(320) for y in range(240)))


if __name__ == '__main__':
    unittest.main()
