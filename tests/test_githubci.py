"""CI GITHUB : alerte quand un run PASSE en echec, poll page affichee seulement."""
import time
import unittest

import helpers
import githubci


def _run(rid, status='completed', conclusion=None):
    return {'databaseId': rid, 'status': status, 'conclusion': conclusion,
            'workflowName': 'ci', 'headBranch': 'main', 'url': 'u'}


def _app():
    with helpers.no_threads():
        app = githubci.GithubCiApp(helpers.FakeDaemon())
    app._cfg_done = True
    app.repos = [{'name': 'a/b', 'runs': [], 'error': None}]
    return app


class AlertTests(unittest.TestCase):
    def test_run_seen_running_then_failing_alerts(self):
        """Le cas le plus courant : un run vu 'en cours' puis termine en echec.
        Avant, il etait marque vu des le 1er poll et l'echec passait inapercu."""
        app = _app()
        app._check_alerts('a/b', [_run(1, 'in_progress', None)])
        self.assertFalse(app.alert_active())      # 1er poll d'un repo : pas d'alerte
        app._check_alerts('a/b', [_run(1, 'completed', 'failure')])
        self.assertTrue(app.alert_active())
        self.assertEqual(app._alert[0], 'b: ECHEC CI')
        first = app._alert
        app._check_alerts('a/b', [_run(1, 'completed', 'failure')])
        self.assertIs(app._alert, first)          # une seule fois par run

    def test_new_failed_run_alerts(self):
        app = _app()
        app._primed.add('a/b')
        app._check_alerts('a/b', [_run(7, 'completed', 'failure')])
        self.assertTrue(app.alert_active())

    def test_success_and_cancelled_dont_alert(self):
        app = _app()
        app._primed.add('a/b')
        app._check_alerts('a/b', [_run(1, 'in_progress'), _run(2, 'queued')])
        app._check_alerts('a/b', [_run(1, 'completed', 'success'), _run(2, 'completed', 'cancelled')])
        self.assertFalse(app.alert_active())

    def test_old_failures_are_forgotten_when_no_longer_listed(self):
        app = _app()
        app._primed.add('a/b')
        app._check_alerts('a/b', [_run(1, 'completed', 'failure')])
        app._check_alerts('a/b', [_run(2, 'completed', 'success')])
        self.assertNotIn(1, app._seen['a/b'])


class PollTests(unittest.TestCase):
    def test_no_poll_before_delay(self):
        app = _app()
        app._next = time.time() + 1000
        with helpers.quiet():
            app._tick()
        self.assertEqual(app.repos[0]['runs'], [])

    def test_status_without_repos(self):
        app = _app()
        app.repos = []
        with helpers.quiet():
            app._tick()
        self.assertIn('aucun repo', app.status)


if __name__ == '__main__':
    unittest.main()
