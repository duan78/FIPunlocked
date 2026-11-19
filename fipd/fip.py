"""
fip.py — Pilote Python du Saitek/Logitech Flight Instrument Panel (VID_06A3 PID_A2AE).

Architecture (clone de DirectOutputSaiFip.dll, RE du 04/11/2026) :
  - Bulk (ecran/LEDs/infos) : interface driver SaiDA2AE {F60E9C9C-...}, handle overlapped RW,
    communications STRICTEMENT sequentielles : write requete -> read reponse (jamais de lecture
    en attente pendant une ecriture — le driver ne le supporte pas).
  - Boutons : interface HID MI_00, handle GENERIC_READ overlapped, thread lecteur dedie.
Protocole bulk :
  Requete  : header 44 octets BIG-ENDIAN (11 x UInt32) + payload par chunks <= 1024
             +0 ServerID(0) +4 Flags(1) +8 PayloadLength +12 RequestID +16 PageID
             +20 PageItemID +24 PayloadID/Valeur +28..43 0
  Reponse  : 44 octets (+ payload) ; length@+8 BE ; RequestID echo@+12 LE (empirique)
  RequestIDs : 1 PING, 3 SAVE, 4 PAYLOAD_DISPLAY, 5 DIRECT, 6 NATIVE, 7 DELETE,
               14 GET_VERSION, 19 CLEAR_SCREEN, 24 SET_LED, 25 SET_LCD
Image : 320x240 24bpp BGR. LED : indices 1..6.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import threading
import time

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
LPVOID = ctypes.c_void_p


class OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_ulonglong), ('InternalHigh', ctypes.c_ulonglong),
                ('Offset', wt.DWORD), ('OffsetHigh', wt.DWORD), ('hEvent', ctypes.c_void_p)]


kernel32.CreateFileW.restype = LPVOID
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, LPVOID, wt.DWORD, wt.DWORD, LPVOID]
kernel32.CreateEventW.restype = LPVOID
kernel32.CreateEventW.argtypes = [LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CloseHandle.argtypes = [LPVOID]
kernel32.ReadFile.argtypes = [LPVOID, LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.argtypes = [LPVOID, LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WaitForSingleObject.argtypes = [LPVOID, wt.DWORD]
kernel32.GetOverlappedResult.argtypes = [LPVOID, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL]
kernel32.DeviceIoControl.argtypes = [LPVOID, wt.DWORD, LPVOID, wt.DWORD, LPVOID, wt.DWORD,
                                     ctypes.POINTER(wt.DWORD), LPVOID]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 3
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997

PING, PAGE_GONE, PAYLOAD_SAVE, PAYLOAD_DISPLAY, DISPLAY_DIRECT, DISPLAY_NATIVE = 1, 2, 3, 4, 5, 6
PAYLOAD_DELETE, PAYLOAD_GET, GET_VERSION, CLEAR_SCREEN, SET_LED, SET_LCD = 7, 8, 14, 19, 24, 25

SOFTBUTTONS = {0x01: 'select', 0x02: 'right_cw', 0x04: 'right_ccw', 0x08: 'left_ccw',
               0x10: 'left_cw', 0x20: 'button1', 0x40: 'button2', 0x80: 'button3',
               0x100: 'button4', 0x200: 'button5', 0x400: 'button6'}


def _enum_iface_paths(iface_guid):
    import winreg
    root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
        rf'SYSTEM\CurrentControlSet\Control\DeviceClasses\{{{iface_guid}}}')
    paths, i = [], 0
    while True:
        try:
            name = winreg.EnumKey(root, i); i += 1
        except OSError:
            break
        if 'vid_06a3&pid_a2ae' in name.lower():
            for p in ('##?#', '\\\\?\\', '##'):
                if name.startswith(p):
                    name = name[len(p):]; break
            paths.append('\\\\?\\' + name)
    return paths


def find_fip_paths():
    return _enum_iface_paths('f60e9c9c-68de-4c21-b2e0-08fb8eea895a')


def find_hid_paths():
    return _enum_iface_paths('4d1e55b2-f16f-11cf-88cb-001111000030')


class FIP:
    def __init__(self, path=None):
        paths = [path] if path else find_fip_paths()
        if not paths:
            raise RuntimeError('interface FIP (driver) introuvable')
        self.path = paths[0]
        self.h = kernel32.CreateFileW(self.path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                                      None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if not self.h or self.h == -1:
            raise RuntimeError(f'CreateFile bulk echoue err={ctypes.get_last_error()}')
        self._lock = threading.RLock()
        # boutons : HID MI_00
        self.on_buttons = None            # callback(state, changed, names)
        self._hid = None
        self._hid_stop = threading.Event()
        self._hid_thread = None
        self._open_hid()

    # ---------- HID boutons ----------

    def _open_hid(self):
        for p in find_hid_paths():
            if 'mi_00' in p.lower():
                h = kernel32.CreateFileW(p, GENERIC_READ, FILE_SHARE_RW,
                                         None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
                if h and h != -1:
                    self._hid = (h, p)
                    break
        if self._hid:
            self._hid_thread = threading.Thread(target=self._hid_loop, daemon=True)
            self._hid_thread.start()

    def _hid_loop(self):
        h = self._hid[0]
        buf = ctypes.create_string_buffer(8)
        while not self._hid_stop.is_set():
            got = wt.DWORD(0)
            ev = kernel32.CreateEventW(None, True, False, None)
            ov = OVERLAPPED()
            ov.hEvent = ev
            ok = kernel32.ReadFile(h, buf, 3, ctypes.byref(got), ctypes.byref(ov))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                kernel32.CloseHandle(ev)
                time.sleep(0.2)
                continue
            r = kernel32.WaitForSingleObject(ev, 2000)
            if r == 0 and kernel32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(got), False):
                data = buf.raw[:got.value]
                if len(data) >= 2 and self.on_buttons:
                    state = data[1] | (data[2] << 8) if len(data) >= 3 else data[1]
                    self._dispatch_buttons(state)
            kernel32.CloseHandle(ev)

    _last_buttons = 0

    def _dispatch_buttons(self, state):
        changed = state ^ self._last_buttons
        self._last_buttons = state
        names = [n for b, n in SOFTBUTTONS.items() if changed & b]
        try:
            self.on_buttons(state, changed, names)
        except Exception:
            pass

    # ---------- bulk : IO sequentielles ----------

    def _ov_read(self, n, timeout_ms):
        buf = ctypes.create_string_buffer(n)
        got = wt.DWORD(0)
        ev = kernel32.CreateEventW(None, True, False, None)
        ov = OVERLAPPED()
        ov.hEvent = ev
        try:
            ok = kernel32.ReadFile(self.h, buf, n, ctypes.byref(got), ctypes.byref(ov))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                return None
            if kernel32.WaitForSingleObject(ev, timeout_ms) != 0:
                kernel32.CancelIoEx(self.h, ctypes.byref(ov))
                kernel32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), False)
                return None
            if kernel32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), False):
                return buf.raw[:got.value]
            return None
        finally:
            kernel32.CloseHandle(ev)

    def _ov_write(self, data, timeout_ms=3000):
        got = wt.DWORD(0)
        ev = kernel32.CreateEventW(None, True, False, None)
        ov = OVERLAPPED()
        ov.hEvent = ev
        try:
            ok = kernel32.WriteFile(self.h, data, len(data), ctypes.byref(got), ctypes.byref(ov))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                return False
            if kernel32.WaitForSingleObject(ev, timeout_ms) != 0:
                kernel32.CancelIoEx(self.h, ctypes.byref(ov))
                kernel32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), False)
                return False
            return bool(kernel32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), False))
        finally:
            kernel32.CloseHandle(ev)

    # ---------- protocole ----------

    @staticmethod
    def _hdr(request, length=0, page=0, item=0, value=0):
        return struct.pack('>11I', 0, 1, length, request, page, item, value, 0, 0, 0, 0)

    def send(self, request, page=0, item=0, value=0, payload=b'', reply_timeout=1500):
        with self._lock:
            if not self._ov_write(self._hdr(request, len(payload), page, item, value)):
                raise RuntimeError(f'write header echoue (req {request})')
            off = 0
            while off < len(payload):
                if not self._ov_write(payload[off:off + 1024], timeout_ms=5000):
                    raise RuntimeError(f'write payload echoue a {off}')
                off += 1024
            return self.read_reply(reply_timeout)

    def read_reply(self, timeout_ms=1500):
        hdr = self._ov_read(44, timeout_ms)
        if not hdr or len(hdr) < 44:
            return None
        length = min(struct.unpack('>I', hdr[8:12])[0], 1_000_000)
        payload = b''
        while len(payload) < length:
            d = self._ov_read(min(length - len(payload), 4096), 3000)
            if not d:
                break
            payload += d
        return {
            'raw': hdr,
            'flags': struct.unpack('>I', hdr[4:8])[0],
            'length': struct.unpack('>I', hdr[8:12])[0],
            'request_le': struct.unpack('<I', hdr[12:16])[0],
            'value': struct.unpack('>I', hdr[24:28])[0],
            'echo_be': struct.unpack('>I', hdr[36:40])[0],
            'payload': payload,
        }

    # ---------- API ----------

    def ping(self):
        return self.send(PING)

    def get_version(self):
        m = self.send(GET_VERSION, reply_timeout=2000)
        return m['payload'].split(b'\x00')[0].decode('ascii', 'replace') if m and m['payload'] else None

    def clear_screen(self, page=0):
        return self.send(CLEAR_SCREEN, page=page, reply_timeout=800)

    def set_brightness(self, v):
        return self.send(SET_LCD, value=v & 0xFF, reply_timeout=800)

    def set_led(self, index, on):
        return self.send(SET_LED, item=index, value=1 if on else 0, reply_timeout=800)

    def display(self, rgb, page=0, item=0, request=PAYLOAD_DISPLAY):
        assert len(rgb) == 320 * 240 * 3
        return self.send(request, page=page, item=item, payload=rgb, reply_timeout=2500)

    def query(self, code, out_size=16):
        out = ctypes.create_string_buffer(out_size)
        ret = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(self.h, code, None, 0, out, out_size, ctypes.byref(ret), None)
        return bool(ok), out.raw[:ret.value]

    def close(self):
        self._hid_stop.set()
        if self._hid:
            kernel32.CloseHandle(self._hid[0])
        kernel32.CloseHandle(self.h)


if __name__ == '__main__':
    fip = FIP()
    print('Bulk:', fip.path)
    print('HID :', fip._hid[1] if fip._hid else 'absent')
    print('Device type:', fip.query(0x220000)[1].hex())
    m = fip.ping()
    print('PING:', m)
    print('Version:', fip.get_version())
    print('Brightness 255:', fip.set_brightness(255))
    print('Clear:', fip.clear_screen())
    row = b'\x00\x00\xff' * 320
    img = row * 240
    r = fip.display(img)
    print('Image rouge:', r if r is None else {k: r[k] for k in ('length', 'request_le')})
    time.sleep(1)
    print('LED 1 ON:', fip.set_led(1, True))
    fip.close()
    print('FIN')
