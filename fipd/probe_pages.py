"""
probe_pages.py — Diagnostic : quels ids de page le FIP accepte-t-il ?
AddPage 0..7 + SetImage sur chacun, hr affiche.
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

os.chdir(r'C:\Program Files\Logitech\DirectOutput')
do = ctypes.WinDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll')
for n in ('DirectOutput_Initialize', 'DirectOutput_AddPage', 'DirectOutput_RemovePage',
          'DirectOutput_SetImage', 'DirectOutput_RegisterPageCallback'):
    getattr(do, n).restype = ctypes.HRESULT
VOID_P = ctypes.c_void_p
do.DirectOutput_AddPage.argtypes = [VOID_P, wt.DWORD, wt.LPCWSTR, wt.DWORD]
do.DirectOutput_RemovePage.argtypes = [VOID_P, wt.DWORD]
do.DirectOutput_SetImage.argtypes = [VOID_P, wt.DWORD, wt.DWORD, wt.DWORD, VOID_P]

ENUM_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
found = []


@ENUM_CB
def on_enum(h, ctx):
    found.append(h)


def hr_str(hr):
    try:
        ctypes.WinError(hr)
        return f'0x{hr & 0xFFFFFFFF:08X} OK'
    except OSError as e:
        return f'0x{hr & 0xFFFFFFFF:08X} {e}'


print('Initialize:', hr_str(do.DirectOutput_Initialize('probe-pages')))
do.DirectOutput_Enumerate(on_enum, None)
h = found[0]
print('device:', hex(h or 0))

img = ctypes.create_string_buffer(b'\x00\x00\xff' * (320 * 240), 320 * 240 * 3)
for pid in (0, 1, 2, 3, 4, 5, 6, 7):
    hr = do.DirectOutput_AddPage(h, pid, f'probe{pid}', 0)
    add = hr_str(hr)
    try:
        time.sleep(0.15)
        do.DirectOutput_SetImage(h, pid, 0, 320 * 240 * 3, img)
        seti = 'OK'
    except OSError as e:
        seti = f'ECHEC {e.winerror & 0xFFFFFFFF:08X}' if e.winerror else str(e)
    print(f'page {pid}: AddPage={add}  SetImage={seti}')
