"""keysend : caracteres resolus par la disposition clavier, touches etendues."""
import unittest

import helpers  # noqa: F401  (chemin)
import keysend


def _capture():
    """Remplace SendInput par un enregistreur : renvoie (liste, fonction a restaurer)."""
    seen = []
    orig = keysend.user32.SendInput
    keysend.user32.SendInput = lambda n, arr, size: (seen.extend(arr[i].ki for i in range(n)), n)[1]
    return seen, orig


def _decode(ki):
    ext = bool(ki.dwFlags & keysend.KEYEVENTF_EXTENDEDKEY)
    up = bool(ki.dwFlags & keysend.KEYEVENTF_KEYUP)
    return (ki.wVk, ki.wScan, ext, up)


class ExtendedKeyTests(unittest.TestCase):
    def test_win_keys_are_extended(self):
        """Sans l'indicateur etendu, 0x5B ne donne PAS la touche Windows."""
        for name in ('win', 'lwin', 'rwin', 'rctrl', 'ralt', 'up', 'left',
                     'volumemute', 'playpause'):
            _, scan, ext = keysend.resolve(name)
            self.assertTrue(ext, name)

    def test_left_modifiers_not_extended(self):
        for name in ('ctrl', 'lctrl', 'shift', 'lshift', 'alt', 'lalt', 'f5', 'enter'):
            _, _, ext = keysend.resolve(name)
            self.assertFalse(ext, name)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            keysend.parse_combo('nope')
        with self.assertRaises(KeyError):
            keysend.parse_combo('ctrl+\u65e5')     # absent de toute disposition latine


class LayoutTests(unittest.TestCase):
    def test_characters_follow_the_active_layout(self):
        """'a' designe le caractere a, quelle que soit la disposition physique."""
        hkl = keysend.current_layout()
        for ch in 'azqwme1':
            with self.subTest(ch=ch):
                vk = keysend.user32.VkKeyScanExW(ch, hkl)
                self.assertNotEqual(vk, -1)
                want = keysend.user32.MapVirtualKeyExW(vk & 0xFF, keysend.MAPVK_VK_TO_VSC, hkl)
                self.assertEqual(keysend.resolve(ch), (ch, want, False))

    def test_azerty_positions(self):
        """Sur un poste AZERTY (040C) : 'a' = touche physique A (0x10), 'q' = Q
        (0x1E), 'z' = Z (0x11, position du W QWERTY), 'm' = 0x27 (la touche
        point-virgule US)."""
        hkl = keysend.current_layout()
        if (hkl & 0xFFFF) != 0x040C:
            self.skipTest('disposition %04X, test AZERTY sans objet' % (hkl & 0xFFFF))
        self.assertEqual(keysend.resolve('a')[1], 0x10)
        self.assertEqual(keysend.resolve('q')[1], 0x1E)
        self.assertEqual(keysend.resolve('z')[1], 0x11)
        self.assertEqual(keysend.resolve('w')[1], 0x2C)
        self.assertEqual(keysend.resolve('m')[1], 0x27)
        self.assertEqual(keysend.resolve('1')[1], 0x02)

    def test_key_names_lists_characters_and_named_keys(self):
        names = keysend.key_names()
        for n in ('win', 'f5', 'volumemute', 'a', 'z', '1', '/'):
            self.assertIn(n, names)


class SendOrderTests(unittest.TestCase):
    def test_release_is_reverse_of_press(self):
        seen, orig = _capture()
        try:
            parts = keysend.parse_combo('ctrl+shift+s')
            keysend.send(parts, up=False)
            keysend.send(parts, up=True)
        finally:
            keysend.user32.SendInput = orig
        down = [k for k in map(_decode, seen) if not k[3]]
        up = [k for k in map(_decode, seen) if k[3]]
        self.assertEqual([k[1] for k in down], [p[1] for p in parts])            # ordre du combo
        self.assertEqual([k[1] for k in up], [p[1] for p in reversed(parts)])    # inverse au relachement
        self.assertTrue(all(k[0] == 0 for k in map(_decode, seen)))               # scancode seul

    def test_blocked_sendinput_raises(self):
        orig = keysend.user32.SendInput
        keysend.user32.SendInput = lambda n, arr, size: 0
        try:
            with self.assertRaises(RuntimeError):
                keysend.tap('f5')
        finally:
            keysend.user32.SendInput = orig


if __name__ == '__main__':
    unittest.main()
