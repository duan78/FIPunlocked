"""CONTEXTE : robustesse du thread de surveillance, confirmations liees a la
fenetre, lectures d'ecran sur la bonne fenetre, fetchs non bloquants, parsers."""
import contextlib
import io
import threading
import time
import unittest
from unittest import mock

import helpers
import contextuel
import fipui
import uiatabs

from PIL import Image, ImageDraw


def _app(**kw):
    with helpers.no_threads():
        app = contextuel.ContextuelApp(helpers.FakeDaemon())
    for k, v in kw.items():
        setattr(app, k, v)
    return app


def _bare(**kw):
    """Une instance sans __init__ (rendus seuls)."""
    app = contextuel.ContextuelApp.__new__(contextuel.ContextuelApp)
    app.d = helpers.FakeDaemon()
    app.kind, app.pl, app.tab, app.scroll = 'app', None, None, 0
    app.proc, app.wtitle, app.hwnd, app.pid, app.exe = 'notepad.exe', 'Sans titre', 0, 0, ''
    app.rect, app.topmost, app._armed = None, False, None
    app._armed_exec = None
    app.repo = app.gh_blob = None
    app.cache = {}
    app.yt, app.fav, app.ddg, app.hfd, app.ghd, app.wiki = {}, {}, {}, {}, {}, {}
    app.pages, app.ediseo, app.gsc = {}, None, None
    app.exp_sel, app.exp_zoom, app._zoom_file = [], False, None
    app._title_t = app._url_t = 0.0
    app.tab_since = 0.0
    app._alert = None
    app.leds = {}
    for k, v in kw.items():
        setattr(app, k, v)
    return app


def _render(app):
    img = Image.new('RGB', (320, 240), fipui.BG)
    app.render_full(img)
    return img


class WatchRobustnessTests(unittest.TestCase):
    def test_malformed_ipv6_url_no_longer_raises(self):
        """Reproduisait : ValueError('Invalid IPv6 URL') -> thread _watch mort,
        cameleon fige jusqu'au reload."""
        app = _app(proc='chrome.exe', hwnd=1)
        app._fg_tab = lambda: {'title': 'Foo', 'url': 'http://[:8080/', 'hwnd': 1}
        kind, pl, _ = app._classify('Foo - Google Chrome')
        self.assertEqual(kind, 'site')
        self.assertEqual(pl, 'http://[:8080/')
        self.assertEqual(contextuel.safe_urlsplit('http://[:x').netloc, '')
        self.assertEqual(contextuel.safe_urlsplit('https://a.b/c').netloc, 'a.b')

    def test_watch_thread_survives_an_exception(self):
        app = _app()
        app._fg_tab = lambda: None
        app._shown.set()                            # page affichée : cadence POLL_S
        calls = []
        state = {'boom': 2}

        def foreground():
            calls.append(1)
            if state['boom']:
                state['boom'] -= 1
                raise ValueError('Invalid IPv6 URL')
            return 'notepad.exe', 'Untitled - Notepad', 0, 0

        t = threading.Thread(target=app._watch, daemon=True)
        with mock.patch.object(contextuel, 'foreground', foreground), \
                contextlib.redirect_stderr(io.StringIO()):
            t.start()
            time.sleep(1.4)
            alive = t.is_alive()
            app._stop.set()
            t.join(3)
        self.assertTrue(alive)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(len(calls), 3)     # la boucle a continue malgre 2 erreurs

    def test_page_entry_wakes_the_watch_and_holds_tabs(self):
        """Page masquée la boucle ralentit (IDLE_S) ; à l'entrée de la page :
        passage immediat + onglets à cadence normale — et rien ne fuit si
        on_enter est rappelé (auto-guérison)."""
        import uiatabs
        app = _app()
        app._fg_tab = lambda: None
        n0 = uiatabs._need
        self.assertGreater(contextuel.IDLE_S, contextuel.POLL_S)
        app._wake.clear()
        app.on_enter()
        self.assertTrue(app._wake.is_set())           # passage immediat
        self.assertEqual(uiatabs._need, n0 + 1)
        app.on_enter()
        self.assertEqual(uiatabs._need, n0 + 1)       # idempotent
        app.on_leave()
        app.on_leave()
        self.assertEqual(uiatabs._need, n0)           # equilibre

    def test_watch_runs_slower_when_hidden(self):
        app = _app()
        app._fg_tab = lambda: None
        calls = []
        with mock.patch.object(contextuel, 'foreground',
                               lambda: (calls.append(1), ('notepad.exe', 'x', 0, 0))[1]):
            t = threading.Thread(target=app._watch, daemon=True)
            t.start()
            time.sleep(1.2)                           # page masquée : IDLE_S = 1 s
            hidden = len(calls)
            app._shown.set()
            app._wake.set()
            time.sleep(1.2)                           # page affichée : POLL_S = 0.4 s
            shown = len(calls) - hidden
            app._stop.set()
            t.join(3)
        self.assertLessEqual(hidden, 2)
        self.assertGreaterEqual(shown, 3)


class ClassifyTests(unittest.TestCase):
    def test_english_wikipedia_title_is_stripped(self):
        app = _app(proc='chrome.exe', hwnd=1, wtitle='Python (programming language) - Wikipedia - Google Chrome')
        app._fg_tab = lambda: {'title': 'Python (programming language) - Wikipedia', 'url': '', 'hwnd': 1}
        kind, pl, _ = app._classify(app.wtitle)
        self.assertEqual((kind, pl), ('wiki', {'lang': 'en', 'title': 'Python (programming language)'}))

    def test_french_wikipedia_title_is_stripped(self):
        app = _app(proc='chrome.exe', hwnd=1, wtitle='Paris — Wikipédia - Google Chrome')
        app._fg_tab = lambda: {'title': 'Paris — Wikipédia', 'url': '', 'hwnd': 1}
        self.assertEqual(app._classify(app.wtitle)[1], {'lang': 'fr', 'title': 'Paris'})

    def test_edge_title_with_soft_hyphen_is_stripped(self):
        app = _app(proc='msedge.exe', hwnd=1, wtitle='Ma page - Microsoft\xad Edge')
        tab = {'title': 'vieux titre', 'url': '', 'hwnd': 1}
        app._fg_tab = lambda: dict(tab)
        self.assertEqual(app._classify(app.wtitle)[2]['title'], 'Ma page')

    def test_title_github_resets_blob(self):
        app = _app(proc='chrome.exe', hwnd=1, wtitle='duan78/FIPunlocked - Google Chrome')
        app._fg_tab = lambda: {'title': 'duan78/FIPunlocked', 'url': '', 'hwnd': 1}
        app.gh_blob = ('main', 'old.py')
        kind, pl, _ = app._classify(app.wtitle)
        self.assertEqual((kind, pl, app.gh_blob), ('github', 'duan78/FIPunlocked', None))


class InvalidTtlTests(unittest.TestCase):
    def test_not_found_is_definitive_other_errors_are_temporary(self):
        self.assertTrue(contextuel._not_found(RuntimeError('HTTP 404: Not Found')))
        self.assertFalse(contextuel._not_found(RuntimeError('timeout')))
        self.assertFalse(contextuel._not_found(RuntimeError('dial tcp: i/o timeout')))

    def test_temporary_invalidity_expires(self):
        app = _app()
        app.invalid['a/b'] = time.time() - contextuel.INVALID_TTL_S - 1
        self.assertFalse(app._is_invalid('a/b'))           # expire : on reessaiera
        self.assertNotIn('a/b', app.invalid)
        app.invalid['c/d'] = time.time()
        self.assertTrue(app._is_invalid('c/d'))            # encore dans le delai
        app.invalid['e/f'] = None
        self.assertTrue(app._is_invalid('e/f'))            # 404 : definitif


class ConfirmTests(unittest.TestCase):
    def test_kill_confirmation_is_bound_to_the_window(self):
        """Si le premier plan change entre les deux appuis, on re-arme au lieu
        de tuer la nouvelle fenetre."""
        app = _app(pid=11)
        self.assertFalse(app._confirm('kill'))      # arme (fenetre 11)
        app.pid = 22                                # le premier plan a change
        self.assertFalse(app._confirm('kill'))      # PAS de confirmation : re-arme (22)
        self.assertTrue(app._confirm('kill'))       # 2e appui sur la meme fenetre : OK

    def test_exec_needs_two_presses_on_the_same_file(self):
        app = _app()
        self.assertFalse(app._confirm_exec('c:/a.py'))
        self.assertFalse(app._confirm_exec('c:/b.py'))
        self.assertFalse(app._confirm_exec('c:/a.py'))
        self.assertTrue(app._confirm_exec('c:/a.py'))


class ClipboardTests(unittest.TestCase):
    def test_banner_tells_the_truth(self):
        app = _app()
        with mock.patch.object(contextuel, 'clipboard_set', return_value=False):
            app._copy('x', 'titre copie')
            self.assertEqual(app._alert[0], 'presse-papiers occupe')
        with mock.patch.object(contextuel, 'clipboard_set', return_value=True):
            app._copy('x', 'titre copie')
            self.assertEqual(app._alert[0], 'titre copie')


class WatchBehaviourTests(unittest.TestCase):
    def _run(self, app, kind, pl, title='Foo - Google Chrome', proc='chrome.exe'):
        app.proc = proc
        app._fg_tab = lambda: {'title': 'Foo', 'url': pl, 'hwnd': 1}
        with mock.patch.object(contextuel, 'foreground',
                               lambda: (proc, title, 1, 4242)), \
                mock.patch.object(app, '_classify', lambda t: (kind, pl, {'title': 'Foo'})), \
                helpers.no_threads():
            app._watch_once()

    def test_youtube_metadata_fetch_does_not_block_the_watch(self):
        """_yt_fetch faisait 2 requetes (12 s max) DANS le thread de surveillance."""
        app = _app()
        started = []
        app._yt_fetch = lambda url: started.append(url)
        self._run(app, 'youtube', 'https://www.youtube.com/watch?v=1')
        self.assertEqual(started, [])                 # pas d'appel bloquant
        self.assertIn('https://www.youtube.com/watch?v=1', app.yt)   # mais reserve

    def test_favicon_fetch_does_not_block_the_watch(self):
        app = _app()
        got = []
        app._fav_fetch = lambda host: got.append(host)
        self._run(app, 'site', 'https://example.com/a')
        self.assertEqual(got, [])
        self.assertIn('example.com', app.fav)

    def test_scroll_resets_on_context_change(self):
        app = _app()
        self._run(app, 'site', 'https://example.com/a')
        app.scroll = 9
        self._run(app, 'site', 'https://example.com/b')
        self.assertEqual(app.scroll, 0)

    def test_ediseo_is_read_once_per_page_never_on_a_timer(self):
        """Avant : relecture + walk UIA (3 s) toutes les 30 s."""
        app = _app()
        self._run(app, 'ediseo', 'https://ediseo/pipeline')
        self.assertIn('ediseo', app._inflight)                 # 1re lecture demandee
        app._inflight.clear()
        app.ediseo = {'t': time.time() - 3600, 'k': 'https://ediseo/pipeline', 'm': {'serper': 1}}
        for _ in range(3):                                     # le temps passe...
            self._run(app, 'ediseo', 'https://ediseo/pipeline')
        self.assertNotIn('ediseo', app._inflight)              # ... toujours aucune relecture
        self._run(app, 'ediseo', 'https://ediseo/pipeline?filter=2')   # autre page :
        self.assertIn('ediseo', app._inflight)                 # nouvelle lecture


class ScreenReadTests(unittest.TestCase):
    def test_the_read_targets_the_window_of_the_request(self):
        """Le job tourne 1-3 s plus tard dans le thread UIA : il lisait
        self.hwnd a CE moment-la, donc parfois une autre fenetre."""
        app = _app(hwnd=111)
        seen = []
        app._uia_texts = lambda hwnd, *a, **k: seen.append(hwnd) or []
        orig = uiatabs.run_in_uia_thread

        def run_later(fn, timeout=8):
            app.hwnd = 999                    # le premier plan a change entre-temps
            return fn()

        uiatabs.run_in_uia_thread = run_later
        try:
            with mock.patch('time.sleep'), helpers.quiet():
                app._fetch_gsc('cle')
                app.hwnd = 111                             # retour sur la fenetre d'origine
                app._fetch_ediseo('cle')
        finally:
            uiatabs.run_in_uia_thread = orig
        self.assertEqual(set(seen), {111})              # jamais la fenetre 999
        self.assertEqual(len(seen), 6)                  # 3 essais x 2 widgets (ecran vide)
        self.assertEqual(app.gsc['err'], None)
        self.assertEqual(app.gsc['m'], {'clics': None, 'impressions': None,
                                        'ctr': None, 'position': None})

    def test_timeout_is_reported_not_confused_with_empty(self):
        app = _app(hwnd=111)
        orig = uiatabs.run_in_uia_thread

        def never(fn, timeout=8):
            raise uiatabs.UiaTimeout('occupe')

        uiatabs.run_in_uia_thread = never
        try:
            app._fetch_gsc('cle')
        finally:
            uiatabs.run_in_uia_thread = orig
        self.assertEqual(app.gsc['err'], 'ecran occupe')


class ParserTests(unittest.TestCase):
    EDISEO = [
        # valeurs fictives (la structure importe : nombre puis label separe)
        '12\u202f000', 'importées', '8\u202f000', 'en production', '6\u202f000', 'générés',
        '4\u202f000', 'livrables',
        'Génération LLM', '③ Génération', '3', 'en cours', '500', 'en file', 'ETA', '1 h 05',
        'Juge LLM', '⚖ Juge', '2', 'en cours', '750', 'en file', 'ETA', '3 j 02 h',
        '9\u202f000', 'crédits serper', 'plafond', '12\u202f000', '09:15:00',
    ]

    def test_ediseo(self):
        m = contextuel.ContextuelApp._parse_ediseo(None, self.EDISEO)
        self.assertEqual((m['importees'], m['production']), (12000, 8000))
        self.assertEqual((m['generes'], m['livrables']), (6000, 4000))
        self.assertEqual((m['gen_cours'], m['gen_file'], m['gen_eta']), (3, 500, '1 h 05'))
        self.assertEqual((m['juge_cours'], m['juge_file'], m['juge_eta']), (2, 750, '3 j 02 h'))
        self.assertEqual((m['serper'], m['serper_max']), (9000, 12000))
        self.assertEqual(m['last_gen'], '09:15:00')

    def test_ediseo_empty_screen_gives_empty(self):
        self.assertEqual(contextuel.ContextuelApp._parse_ediseo(None, ['Menu', 'Accueil']), {})

    def test_ediseo_real_labels(self):
        """Structure relevee sur le tableau de bord reel (23/11), valeurs
        fictives : le libelle d'import est 'URLs importées', suivi de
        '· CSV complet'."""
        texts = ['Tableau de bord', 'pipeline auto :', 'actif',
                 '12\u202f000', 'URLs importées', '· CSV complet', '?', 'une longue description',
                 'pilote :', '500', '· périmètre client :', '20\u202f000',
                 '8\u202f000', 'En production', '· pipeline', '?',
                 'Génération LLM', '0', 'en cours', '0', 'en file', 'ETA', '45 min',
                 'Juge LLM', '4', 'en cours', '120', 'en file', 'ETA', '2 h 30 min', '16:40:12']
        m = contextuel.ContextuelApp._parse_ediseo(None, texts)
        self.assertEqual((m['importees'], m['production']), (12000, 8000))
        self.assertEqual((m['gen_eta'], m['juge_eta']), ('45 min', '2 h 30 min'))
        self.assertEqual((m['juge_cours'], m['juge_file'], m['last_gen']), (4, 120, '16:40:12'))
        self.assertNotIn('serper', m)          # le bloc de credits a disparu de la page

    def test_gsc(self):
        texts = ['Nombre total de clics', '250\u202fk', "Nombre total d'impressions",
                 '31,2\u202fM', 'CTR moyen', '2,7\u202f%', 'Position moyenne', '12,5']
        m = contextuel.ContextuelApp._parse_gsc(None, texts)
        self.assertEqual(m, {'clics': '250\u202fk', 'impressions': '31,2\u202fM',
                             'ctr': '2,7\u202f%', 'position': '12,5'})


class RenderTests(unittest.TestCase):
    METRICS = {'importees': 12000, 'production': 8000, 'generes': 6000, 'livrables': 4000,
               'gen_cours': 3, 'gen_file': 500, 'gen_eta': '1 h 05', 'juge_cours': 2,
               'juge_file': 750, 'juge_eta': '3 j 02 h', 'serper': 9000,
               'serper_max': 12000, 'last_gen': '09:15:00'}

    def test_every_context_renders(self):
        cases = [
            dict(kind='ediseo', ediseo={'t': 0, 'k': '', 'm': self.METRICS}),
            dict(kind='ediseo', ediseo=None),
            dict(kind='ediseo', ediseo={'t': 0, 'k': '', 'm': {}, 'err': 'ecran occupe'}),
            dict(kind='gsc', gsc={'t': 0, 'k': '', 'm': {'clics': '250 k'}, 'err': None}),
            dict(kind='gsc', gsc=None),
            dict(kind='gsc', gsc={'t': 0, 'k': '', 'm': {}, 'err': 'ecran occupe'}),
            dict(kind='site', pl='https://example.com/a',
                 tab={'title': 'Un titre de page assez long pour tenir sur deux lignes'}),
            dict(kind='wiki', pl={'lang': 'en', 'title': 'Python'},
                 wiki={('en', 'Python'): {'t': 0, 'data': {'extract': 'x ' * 900}, 'img': None}},
                 scroll=500),
            dict(kind='app', wtitle='x ' * 200, scroll=500),
            dict(kind='youtube', pl='https://youtu.be/1', tab={'title': 'Video - YouTube'}),
            dict(kind='gmail', tab={'title': '(1 234) Boîte de réception - x@y.fr'}),
            dict(kind='hf', pl='microsoft/phi-4'),
            dict(kind='ghpage', pl={'repo': 'a/b', 'kind': 'pull', 'n': 12}),
            dict(kind='explorer', pl='C:\\Users'),
            dict(kind='code', pl={'file': 'a.py', 'ws': 'w'}),
            dict(kind='terminal', pl={'cwd': None}),
            dict(kind='vlc', pl={'name': 'film.mkv'}),
            dict(kind='github', repo='duan78/FIPunlocked'),
            dict(kind='github', repo=None),
            dict(kind='gsearch', pl='https://www.google.com/search?q=3*1.25', scroll=0),
            dict(kind='chat', pl='https://chat.z.ai/', tab={'title': 'Conversation'}),
        ]
        for case in cases:
            with self.subTest(case=case.get('kind'), pl=str(case.get('pl'))[:30]):
                img = _render(_bare(**case))
                self.assertEqual(img.size, (320, 240))

    def test_scrolling_past_the_end_still_shows_text(self):
        app = _bare(kind='app', wtitle='Titre de fenetre long ' * 10, scroll=10 ** 6)
        img = _render(app)
        # du texte (pixels clairs) dans la zone du titre, sous y=104
        px = img.load()
        bright = sum(1 for y in range(104, 220) for x in range(106, 313)
                     if sum(px[x, y]) > 600)
        self.assertGreater(bright, 200)

    def test_ediseo_values_end_inside_the_panel(self):
        """Les valeurs de l'entonnoir etaient coupees ('120' au lieu de 12 000)."""
        app = _bare(kind='ediseo', ediseo={'t': 0, 'k': '', 'm': self.METRICS})
        _render(app)
        f = fipui.font(11)
        d = ImageDraw.Draw(Image.new('RGB', (10, 10)))
        w = d.textlength('12 000', font=f)
        self.assertGreaterEqual(313 - w, 266)      # ne chevauche pas les barres (fin a 266)


if __name__ == '__main__':
    unittest.main()
