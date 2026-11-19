"""
replay_session.py — Rejoue la session SPAD->FIP capturee (spad_session.log du 05/11).

Sequence bulk (d'apres capture Frida) :
  1. open bulk {f60e9c9c} (x2 handles chez SPAD)
  2. ioctl 0x220004 (serie)
  3. W: GET_VERSION header 44o  [0,0,0,14,0...]
  4. R: reponse
  5. display: W header 44o [0,3,230400,0,0x60,0...] puis W image 230400o, puis R
  6. HID : ioctls HidD (0xb01a8 x2, 0xb0193, 0xb01be, 0xb01ba) puis ReadFile en boucle
"""
import ctypes
import struct
import threading
import time

import fip as raw

import struct as _s
HDR_GETVERSION = _s.pack('>11I', 0, 0, 0, 14, 0, 0, 0, 0, 0, 0, 0)
HDR_DISPLAY = _s.pack('>11I', 0, 3, 230400, 0, 0x60, 0, 0, 0, 0, 0, 0)


def main():
    # --- handle bulk ---
    bulk_paths = raw.find_fip_paths()
    hb = raw.kernel32.CreateFileW(bulk_paths[0], 0xC0000000, 3, None, 3,
                                  raw.FILE_FLAG_OVERLAPPED, None)
    print('bulk handle:', hb)

    # ioctl serie (comme SPAD)
    out = ctypes.create_string_buffer(128)
    ret = raw.wt.DWORD(0)
    ok = raw.kernel32.DeviceIoControl(hb, 0x220004, None, 0, out, 128, ctypes.byref(ret), None)
    print('ioctl serie:', ok, out.raw[:20].hex(' '))

    # --- GET_VERSION ---
    print('write GET_VERSION:', raw.FIP._write_ov if False else '', end='')
    got = raw.wt.DWORD(0)
    ev = raw.kernel32.CreateEventW(None, True, False, None)
    ov = raw.OVERLAPPED()
    ov.hEvent = ev
    ok = raw.kernel32.WriteFile(hb, HDR_GETVERSION, 44, ctypes.byref(got), ctypes.byref(ov))
    r = raw.kernel32.WaitForSingleObject(ev, 3000)
    raw.kernel32.GetOverlappedResult(hb, ctypes.byref(ov), ctypes.byref(got), False)
    raw.kernel32.CloseHandle(ev)
    print(f'ok={ok} r={r} written={got.value}')

    # lire la reponse (drain)
    buf = ctypes.create_string_buffer(512)
    ev = raw.kernel32.CreateEventW(None, True, False, None)
    ov = raw.OVERLAPPED()
    ov.hEvent = ev
    ok = raw.kernel32.ReadFile(hb, buf, 512, ctypes.byref(got), ctypes.byref(ov))
    r = raw.kernel32.WaitForSingleObject(ev, 1500)
    raw.kernel32.GetOverlappedResult(hb, ctypes.byref(ov), ctypes.byref(got), False)
    raw.kernel32.CloseHandle(ev)
    print(f'reponse GET_VERSION: r={r} lu={got.value} {buf.raw[:min(got.value,48)].hex(" ")}')

    # --- display : notre image (rouge) ---
    img = b'\x00\x00\xff' * (320 * 240)

    def send_display(image):
        for blob in (HDR_DISPLAY, image):
            ev = raw.kernel32.CreateEventW(None, True, False, None)
            ov = raw.OVERLAPPED()
            ov.hEvent = ev
            ok = raw.kernel32.WriteFile(hb, blob, len(blob), ctypes.byref(got), ctypes.byref(ov))
            r = raw.kernel32.WaitForSingleObject(ev, 5000)
            raw.kernel32.GetOverlappedResult(hb, ctypes.byref(ov), ctypes.byref(got), False)
            raw.kernel32.CloseHandle(ev)
            if r != 0:
                return False
        # lecture de la reponse (comme SPAD)
        ev = raw.kernel32.CreateEventW(None, True, False, None)
        ov = raw.OVERLAPPED()
        ov.hEvent = ev
        raw.kernel32.ReadFile(hb, buf, 512, ctypes.byref(got), ctypes.byref(ov))
        r = raw.kernel32.WaitForSingleObject(ev, 1000)
        raw.kernel32.GetOverlappedResult(hb, ctypes.byref(ov), ctypes.byref(got), False)
        raw.kernel32.CloseHandle(ev)
        return True

    print('display rouge:', send_display(img))
    time.sleep(1)
    # 2e frame (comme SPAD qui envoie 3-4 frames)
    img2 = b'\x00\xff\x00' * (320 * 240)
    print('display vert:', send_display(img2))

    # --- thread boutons HID (avec l init ioctl SPAD) ---
    stop = threading.Event()
    reports = []

    def hid_thread():
        paths = [p for p in raw.find_hid_paths() if 'mi_00' in p.lower()]
        hh = raw.kernel32.CreateFileW(paths[0], raw.GENERIC_READ, 3, None, 3,
                                      raw.FILE_FLAG_OVERLAPPED, None)
        # ioctls HidD comme SPAD (0xb01a8 x2, 0xb0193 caps, 0xb01be produit, 0xb01ba serie)
        for code, sz in ((0xb01a8, 12), (0xb01a8, 12), (0xb0193, 268), (0xb01a8, 12),
                         (0xb0193, 268), (0xb01be, 256), (0xb01ba, 256)):
            o = ctypes.create_string_buffer(sz)
            rt = raw.wt.DWORD(0)
            raw.kernel32.DeviceIoControl(hh, code, None, 0, o, sz, ctypes.byref(rt), None)
        g = raw.wt.DWORD(0)
        while not stop.is_set():
            b2 = ctypes.create_string_buffer(8)
            e2 = raw.kernel32.CreateEventW(None, True, False, None)
            o2 = raw.OVERLAPPED()
            o2.hEvent = e2
            raw.kernel32.ReadFile(hh, b2, 3, ctypes.byref(g), ctypes.byref(o2))
            rr = raw.kernel32.WaitForSingleObject(e2, 300)
            res = raw.kernel32.GetOverlappedResult(hh, ctypes.byref(o2), ctypes.byref(g), False)
            raw.kernel32.CloseHandle(e2)
            if rr == 0 and g.value:
                reports.append(b2.raw[:g.value])
                print('*** HID EVENT:', b2.raw[:g.value].hex(' '), flush=True)
        raw.kernel32.CloseHandle(hh)

    t = threading.Thread(target=hid_thread, daemon=True)
    t.start()

    print('=' * 60)
    print('>>> SESSION SPAD REJOUEE. REGARDEZ L ECRAN + TOURNEZ KNOB/BOUTONS (25 s) <<<')
    time.sleep(25)
    stop.set()
    print(f'TOTAL HID: {len(reports)} events')
    raw.kernel32.CloseHandle(hb)


if __name__ == '__main__':
    main()
