"""Commun aux tests : chemin du daemon + garde-fous (aucun thread, aucune touche)."""
import contextlib
import os
import sys
import threading

FIPD = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'fipd')
sys.path.insert(0, os.path.abspath(FIPD))
sys.path.insert(0, os.path.join(os.path.abspath(FIPD), 'apps'))


@contextlib.contextmanager
def no_threads():
    """Empeche les apps de lancer leurs threads pendant leur construction."""
    orig = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        yield
    finally:
        threading.Thread.start = orig


@contextlib.contextmanager
def quiet():
    """Absorbe les prints du daemon (traces [page], [apps]...)."""
    with contextlib.redirect_stdout(None):
        yield


class FakeDaemon:
    """Daemon minimal pour les apps seules (pas de peripherique)."""

    def __init__(self, apps=()):
        self.apps = list(apps)
        self.page = 0
        self.mode = 'ui'
        self.leds = {}
        self.redraws = 0

    def set_led(self, index, on):
        self.leds[index] = bool(on)
        return 0

    def _redraw_now(self):
        self.redraws += 1
