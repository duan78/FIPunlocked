"""
notrefirmware.py — Prise de controle complete du FIP : notre session, notre boucle.

D'apres la capture Frida de SPAD (05/11) :
  BULK (MI_01 {f60e9c9c}) :
    ioctl 0x220004 (serie) -> GET_VERSION [0,0,0,14,...] -> read reponse
    display: write [0,3,230400,0,0x60,0...] puis image 230400, puis read reponse
  HID (MI_00 {4d1e55b2}) :
    ioctls HidD (b01a8 x2, b0193, b01a8, b0193, b01be, b01ba)
    puis BOUCLE SERRLEE ReadFile(4096) — 65/s — c'est le canal boutons !
"""
import ctypes
import struct
import threading
import time

import fip as raw

k32 = raw.kernel32


class NotreFirmware:
    def __init__(self):
        self.hb = k32.CreateFileW(raw.find_fip_paths()[0], 0xC0000000, 3, None, 3,
                                  raw.FILE_FLAG_OVERLAPPED, None)
        self.got = raw.wt.DWORD(0)
        self.buf = ctypes.create_string_buffer(8192)
        self.on_buttons = None
        self._last = 0
        self._stop = threading.Event()

    # ---- bulk ----
    def _wr(self, blob, t=10000):
        ev = k32.CreateEventW(None, True, False, None)
        ov = raw.OVERLAPPED(); ov.hEvent = ev
        k32.WriteFile(self.hb, blob, len(blob), ctypes.byref(self.got), ctypes.byref(ov))
        r = k32.WaitForSingleObject(ev, t)
        k32.GetOverlappedResult(self.hb, ctypes.byref(ov), ctypes.byref(self.got), False)
        k32.CloseHandle(ev)
        return r == 0

    def _rd(self, t=2500):
        ev = k32.CreateEventW(None, True, False, None)
        ov = raw.OVERLAPPED(); ov.hEvent = ev
        k32.ReadFile(self.hb, self.buf, 8192, ctypes.byref(self.got), ctypes.byref(ov))
        r = k32.WaitForSingleObject(ev, t)
        k32.GetOverlappedResult(self.hb, ctypes.byref(ov), ctypes.byref(self.got), False)
        k32.CloseHandle(ev)
        return self.buf.raw[:self.got.value] if r == 0 else None

    def session(self):
        out = ctypes.create_string_buffer(128)
        k32.DeviceIoControl(self.hb, 0x220004, None, 0, out, 128,
                            ctypes.byref(self.got), None)
        self._wr(struct.pack('>11I', 0, 0, 0, 14, 0, 0, 0, 0, 0, 0, 0))
        self._rd()
        return True

    def display(self, image_bgr):
        assert len(image_bgr) == 230400
        hdr = struct.pack('>11I', 0, 3, 230400, 0, 0x60, 0, 0, 0, 0, 0, 0)
        ok = self._wr(hdr) and self._wr(image_bgr)
        self._rd(2500)
        return ok

    # ---- boutons : boucle HID seree comme SPAD ----
    def start_buttons(self):
        paths = [p for p in raw.find_hid_paths() if 'mi_00' in p.lower()]
        hh = k32.CreateFileW(paths[0], raw.GENERIC_READ, 3, None, 3,
                             raw.FILE_FLAG_OVERLAPPED, None)
        # init HidD a la SPAD
        for code, sz in ((0xb01a8, 12), (0xb01a8, 12), (0xb0193, 268), (0xb01a8, 12),
                         (0xb0193, 268), (0xb01be, 256), (0xb01ba, 256)):
            o = ctypes.create_string_buffer(sz)
            k32.DeviceIoControl(hh, code, None, 0, o, sz, ctypes.byref(self.got), None)

        def loop():
            g = raw.wt.DWORD(0)
            b = ctypes.create_string_buffer(4096)
            ev = k32.CreateEventW(None, False, False, None)   # auto-reset, REUTILISE
            while not self._stop.is_set():
                ov = raw.OVERLAPPED()
                ov.hEvent = ev
                k32.ReadFile(hh, b, 4096, ctypes.byref(g), ctypes.byref(ov))
                r = k32.WaitForSingleObject(ev, 120)
                if r != 0:
                    time.sleep(0.005)
                res = k32.GetOverlappedResult(hh, ctypes.byref(ov), ctypes.byref(g), False)
                if r == 0 and res and g.value >= 2:
                    state = b.raw[1] | (b.raw[2] << 8)
                    changed = state ^ self._last
                    self._last = state
                    if changed and self.on_buttons:
                        names = [n for bb, n in raw.SOFTBUTTONS.items() if changed & bb]
                        try:
                            self.on_buttons(state, changed, names)
                        except Exception:
                            pass
            k32.CloseHandle(ev)
            k32.CloseHandle(hh)

        threading.Thread(target=loop, daemon=True).start()


if __name__ == '__main__':
    fw = NotreFirmware()
    print('session:', fw.session())
    img = b'\x00\x00\xff' * (320 * 240)
    print('display rouge:', fw.display(img))
    fw.on_buttons = lambda s, c, n: print(f'*** BOUTON state=0x{s:03X} {n}', flush=True)
    fw.start_buttons()
    print('>>> NOTRE SESSION ACTIVE — TOURNEZ KNOBS + BOUTONS (30 s) <<<', flush=True)
    t0 = time.time()
    n = [0]
    orig = fw.on_buttons
    def count(s, c, nm):
        n[0] += 1
        orig(s, c, nm)
    fw.on_buttons = count
    while time.time() - t0 < 30:
        if int(time.time() - t0) % 10 == 0:
            fw.display(img)      # keepalive display
        time.sleep(0.2)
    print('TOTAL EVENTS:', n[0])
