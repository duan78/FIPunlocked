"""
keysend.py — Envoi de raccourcis clavier par SendInput (scancodes), pour fipd3.
Supporte les modificateux maintenus (combo down/up) et les touches etendues/media.
"""
import ctypes
import ctypes.wintypes as wt

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1


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

# name -> (scancode, extended) ; E0xx = extended
_SCAN = {
    **{c: (0x10 + ord(c) - ord('q'), False) for c in 'qwertyuiop'},
    **{c: (0x1E + ord(c) - ord('a'), False) for c in 'asdfghjkl'},
    **{c: (0x2C + ord(c) - ord('z'), False) for c in 'zxcvbnm'},
    **{str(d): (0x02 + d - 1, False) for d in range(1, 10)},
    '0': (0x0B, False),
    'f1': (0x3B, False), 'f2': (0x3C, False), 'f3': (0x3D, False), 'f4': (0x3E, False),
    'f5': (0x3F, False), 'f6': (0x40, False), 'f7': (0x41, False), 'f8': (0x42, False),
    'f9': (0x43, False), 'f10': (0x44, False), 'f11': (0x57, False), 'f12': (0x58, False),
    'enter': (0x1C, False), 'return': (0x1C, False), 'esc': (0x01, False),
    'escape': (0x01, False), 'backspace': (0x0E, False), 'tab': (0x0F, False),
    'space': (0x39, False), 'capslock': (0x3A, False),
    'ctrl': (0x1D, False), 'lctrl': (0x1D, False), 'rctrl': (0x1D, True),
    'shift': (0x2A, False), 'lshift': (0x2A, False), 'rshift': (0x36, False),
    'alt': (0x38, False), 'lalt': (0x38, False), 'ralt': (0x38, True),
    'win': (0x5B, False), 'lwin': (0x5B, False), 'rwin': (0x5C, True),
    '-': (0x0C, False), '=': (0x0D, False), '[': (0x1A, False), ']': (0x1B, False),
    ';': (0x27, False), "'": (0x28, False), '`': (0x29, False),
    ',': (0x33, False), '.': (0x34, False), '/': (0x35, False), '\\': (0x2B, False),
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


def key_names():
    return sorted(_SCAN)


def _mk(scan, extended, up):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(0, scan & 0xFF,
                        KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0)
                        | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    return inp


def parse_combo(text):
    """'ctrl+shift+s' -> [('ctrl',...), ('shift',...), ('s',...)] ; KeyError si inconnu."""
    parts = [p.strip().lower() for p in text.split('+') if p.strip()]
    if not parts:
        raise ValueError('combo vide')
    return [(p, *_SCAN[p]) for p in parts]


def send(parts, up=False):
    arr = (INPUT * len(parts))(*[_mk(sc, ext, up) for _, sc, ext in parts])
    return user32.SendInput(len(parts), arr, ctypes.sizeof(INPUT))


def tap(text):
    """Combo complet : appui puis relachement."""
    parts = parse_combo(text)
    send(parts, up=False)
    send(parts, up=True)


def combo_down(parts):
    send(parts, up=False)


def combo_up(parts):
    send(parts, up=True)
