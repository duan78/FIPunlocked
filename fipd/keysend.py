"""
keysend.py — Envoi de raccourcis clavier par SendInput (scancodes), pour fipd3.
Supporte les modificateurs maintenus (combo down/up) et les touches etendues/media.

Deux familles de touches :
  - IMPRIMABLES (lettres, chiffres, signes) : résolues par la disposition
    clavier COURANTE (VkKeyScanEx + MapVirtualKeyEx). 'a' désigne donc le
    caractère a, que le clavier soit AZERTY, QWERTY ou autre — les scancodes
    physiques changent, pas la touche visée. Un caractère est envoyé SANS
    Shift : 'ctrl+1' reste le raccourci « touche 1 », même si le 1 demande
    Shift sur la disposition.
  - NOMMÉES (f1, up, win, volumemute...) : table fixe ; celles qui le
    nécessitent portent l'indicateur étendu (win, rctrl, flèches, media...).
Les touches sont relâchées dans l'ordre inverse de l'appui.
"""
import ctypes
import ctypes.wintypes as wt
import string

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
MAPVK_VK_TO_VSC = 0


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', wt.DWORD), ('u', _INPUTUNION),
                ('_pad', ctypes.c_byte * 8)]   # alignement x64 (sizeof=40)


user32 = ctypes.WinDLL('user32')
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetKeyboardLayout.argtypes = [wt.DWORD]
user32.GetKeyboardLayout.restype = wt.HANDLE
user32.MapVirtualKeyExW.argtypes = [wt.UINT, wt.UINT, wt.HANDLE]
user32.MapVirtualKeyExW.restype = wt.UINT
user32.VkKeyScanExW.argtypes = [wt.WCHAR, wt.HANDLE]
user32.VkKeyScanExW.restype = ctypes.c_short

# name -> (scancode, extended) ; True = touche etendue (E0)
_NAMED = {
    'f1': (0x3B, False), 'f2': (0x3C, False), 'f3': (0x3D, False), 'f4': (0x3E, False),
    'f5': (0x3F, False), 'f6': (0x40, False), 'f7': (0x41, False), 'f8': (0x42, False),
    'f9': (0x43, False), 'f10': (0x44, False), 'f11': (0x57, False), 'f12': (0x58, False),
    'enter': (0x1C, False), 'return': (0x1C, False), 'esc': (0x01, False),
    'escape': (0x01, False), 'backspace': (0x0E, False), 'tab': (0x0F, False),
    'space': (0x39, False), 'capslock': (0x3A, False),
    'ctrl': (0x1D, False), 'lctrl': (0x1D, False), 'rctrl': (0x1D, True),
    'shift': (0x2A, False), 'lshift': (0x2A, False), 'rshift': (0x36, False),
    'alt': (0x38, False), 'lalt': (0x38, False), 'ralt': (0x38, True),
    'win': (0x5B, True), 'lwin': (0x5B, True), 'rwin': (0x5C, True),
    'up': (0x48, True), 'down': (0x50, True), 'left': (0x4B, True), 'right': (0x4D, True),
    'pageup': (0x49, True), 'pgup': (0x49, True), 'pagedown': (0x51, True),
    'pgdn': (0x51, True), 'home': (0x47, True), 'end': (0x4F, True),
    'insert': (0x52, True), 'delete': (0x53, True), 'del': (0x53, True),
    'printscreen': (0x37, True), 'prtsc': (0x37, True),
    'volumemute': (0x20, True), 'volumeup': (0x30, True), 'volumedown': (0x2E, True),
    'playpause': (0x22, True), 'mediastop': (0x24, True),
    'medianext': (0x19, True), 'mediaprev': (0x10, True),
    'npenter': (0x1C, True),
}
_CHARS = string.ascii_lowercase + string.digits + "-=[]\\;',./`"


def key_names():
    """Touches reconnues : nommées + caractères imprimables (résolus par la disposition)."""
    return sorted(_NAMED) + list(_CHARS)


def current_layout():
    """Disposition clavier du thread de la fenêtre au premier plan (repli :
    celle du thread courant)."""
    try:
        h = user32.GetForegroundWindow()
        if h:
            hkl = user32.GetKeyboardLayout(user32.GetWindowThreadProcessId(h, None))
            if hkl:
                return hkl
    except Exception:
        pass
    return user32.GetKeyboardLayout(0)


def resolve(name):
    """name -> (name, scancode, etendue). KeyError si absent de la disposition."""
    if len(name) == 1:
        hkl = current_layout()
        vk = user32.VkKeyScanExW(name, hkl)
        sc = user32.MapVirtualKeyExW(vk & 0xFF, MAPVK_VK_TO_VSC, hkl) if vk != -1 else 0
        if not sc:
            raise KeyError('touche absente de la disposition clavier : %r' % name)
        return (name, sc, False)
    sc, ext = _NAMED[name]                # KeyError si inconnue
    return (name, sc, ext)


def _mk(scan, extended, up):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(0, scan & 0xFF,
                        KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0)
                        | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    return inp


def parse_combo(text):
    """'ctrl+shift+s' -> [('ctrl',...), ('shift',...), ('s',...)] ; KeyError si inconnue."""
    parts = [p.strip().lower() for p in text.split('+') if p.strip()]
    if not parts:
        raise ValueError('combo vide')
    return [resolve(p) for p in parts]


def send(parts, up=False):
    """Envoie les touches : appui dans l'ordre du combo, relâchement en ordre
    inverse (la touche d'abord, les modificateurs ensuite)."""
    seq = list(reversed(parts)) if up else parts
    arr = (INPUT * len(seq))(*[_mk(sc, ext, up) for _, sc, ext in seq])
    n = user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
    if n != len(seq):
        raise RuntimeError('SendInput refuse (%d/%d) — fenêtre élevée ?' % (n, len(seq)))
    return n


def tap(text):
    """Combo complet : appui puis relachement."""
    parts = parse_combo(text)
    send(parts, up=False)
    send(parts, up=True)


def combo_down(parts):
    send(parts, up=False)


def combo_up(parts):
    send(parts, up=True)
