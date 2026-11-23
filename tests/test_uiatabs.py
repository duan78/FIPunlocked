"""uiatabs : suffixes de navigateur, delai explicite du thread UIA, purge des caches."""
import threading
import time
import unittest

import helpers  # noqa: F401  (chemin)
import uiatabs


class SuffixTests(unittest.TestCase):
    def test_product_suffix_is_stripped(self):
        cases = {
            'Ma page - Google Chrome': 'Ma page',
            'Ma page - Microsoft\xad Edge': 'Ma page',       # tiret mou (Edge)
            'Ma page - Microsoft\u200b Edge': 'Ma page',     # espace sans largeur
            'Ma page - Microsoft Edge': 'Ma page',
            'Titre — Mozilla Firefox': 'Titre',
            'Titre - Brave': 'Titre',
            'Titre - Opera': 'Titre',
            'Titre - Vivaldi': 'Titre',
            'duan78/FIPunlocked - Google Chrome': 'duan78/FIPunlocked',
        }
        for title, want in cases.items():
            with self.subTest(title=title):
                self.assertEqual(uiatabs.strip_suffix(title), want)

    def test_plain_title_untouched(self):
        for title in ('Sans titre - Bloc-notes', 'Rapport - Q4', '', 'Opéra de Sydney'):
            self.assertEqual(uiatabs.strip_suffix(title), title)

    def test_browsers_cover_the_suffixes(self):
        self.assertEqual(set(uiatabs.BROWSERS),
                         {'chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe',
                          'opera.exe', 'vivaldi.exe'})


class _HiddenQueue(list):
    """File que le thread UIA croit VIDE (test du delai deterministe : le vrai
    thread, lance par d'autres tests du meme processus, ne doit pas consommer),
    mais que le test lit normalement."""
    def __bool__(self):
        return False


class JobTests(unittest.TestCase):
    def setUp(self):
        self._real = uiatabs._jobs
        uiatabs._jobs = _HiddenQueue()

    def tearDown(self):
        uiatabs._jobs = self._real
        uiatabs._jobs.clear()

    def test_timeout_is_explicit_and_cancels_the_job(self):
        """Avant : None au bout du delai, indistinguable d'une lecture vide."""
        t0 = time.time()
        with self.assertRaises(uiatabs.UiaTimeout):
            uiatabs.run_in_uia_thread(lambda: 42, timeout=0.2)   # personne ne peut consommer
        self.assertLess(time.time() - t0, 3)
        self.assertTrue(all(job[2] for job in uiatabs._jobs))     # annulee : ne
        self.assertEqual(len(uiatabs._jobs), 1)                   # tournera pas plus tard

    def _serve_one(self):
        """Joue le role du thread UIA pour UN job."""
        for _ in range(200):
            if uiatabs._jobs:
                fn, q, cancelled = uiatabs._jobs.pop(0)
                if not cancelled:
                    try:
                        q.put(fn())
                    except Exception as e:
                        q.put(e)
                return
            time.sleep(0.01)

    def test_result_and_errors_come_back(self):
        uiatabs._jobs = self._real           # ici on joue le consommateur sois-meme
        out = []
        t1 = threading.Thread(target=lambda: out.append(
            uiatabs.run_in_uia_thread(lambda: 'fait', timeout=5)))
        t1.start()
        self._serve_one()
        t1.join(3)
        self.assertEqual(out, ['fait'])

        def boom():
            raise ValueError('panne')

        errs = []

        def call():
            try:
                uiatabs.run_in_uia_thread(boom, timeout=5)
                errs.append('pas derreur')
            except ValueError as e:
                errs.append(str(e))

        t2 = threading.Thread(target=call)
        t2.start()
        self._serve_one()
        t2.join(3)
        self.assertEqual(errs, ['panne'])     # l'erreur de fn est relayee a l'appelant


class CadenceTests(unittest.TestCase):
    def test_acquire_release_count_pages_and_wake_the_thread(self):
        """Page masquée le thread UIA ralentit (IDLE_S) ; à l'entrée d'une page
        qui lit les onglets : cadence normale + passe immédiate."""
        n0, w0 = uiatabs._need, uiatabs._wake.is_set()
        try:
            uiatabs._wake.clear()
            self.assertGreater(uiatabs.IDLE_S, uiatabs.UIA_S)
            uiatabs._need = 0
            uiatabs.acquire()
            self.assertEqual(uiatabs._need, 1)
            self.assertTrue(uiatabs._wake.is_set())     # passe immédiate
            uiatabs.acquire()
            self.assertEqual(uiatabs._need, 2)
            uiatabs.release()
            uiatabs.release()
            uiatabs.release()                           # jamais négatif
            self.assertEqual(uiatabs._need, 0)
        finally:
            uiatabs._need, uiatabs._wake = n0, w0 and uiatabs._wake or None
            if not w0:
                uiatabs._wake.clear()


class CacheTests(unittest.TestCase):
    def test_purge_drops_what_no_longer_exists(self):
        """Les hwnd/pid sont REUTILISES par Windows : un cache jamais purge
        finissait par designer une autre fenetre."""
        uiatabs._el_cache[1] = {}
        uiatabs._el_cache[2] = {}
        uiatabs._last_state[2] = {}
        uiatabs._last_url[3] = ('u', 0)
        uiatabs._proc_cache[10] = 'chrome.exe'
        uiatabs._proc_cache[11] = 'chrome.exe'
        uiatabs._purge({2}, {11})
        self.assertEqual(list(uiatabs._el_cache), [2])
        self.assertEqual(list(uiatabs._last_state), [2])
        self.assertEqual(uiatabs._last_url, {})
        self.assertEqual(uiatabs._proc_cache, {11: 'chrome.exe'})
        uiatabs._el_cache.clear()
        uiatabs._last_state.clear()
        uiatabs._proc_cache.clear()

    def test_browser_windows_shape(self):
        for w in uiatabs._browser_windows():
            self.assertEqual(len(w), 4)
            self.assertIn(w[2], uiatabs.BROWSERS)


if __name__ == '__main__':
    unittest.main()
