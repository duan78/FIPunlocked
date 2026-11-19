"""
probe_pages2.py — Matrice : quel couple (AddPage, SetImage) fonctionne ?
Cas A : id 0 ACTIVE (config historique qui marchait)
Cas B : id 1 ACTIVE seul
Cas C : 1 ACTIVE puis bascule vers 2 ACTIVE
Cas D : ids 1..5, seul 1 ACTIVE (config daemon)
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

os.chdir(r'C:\Program Files\Logitech\DirectOutput')
do = ctypes.WinDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll')
for n in ('DirectOutput_Initialize', 'DirectOutput_AddPage', 'DirectOutput_RemovePage',
          'DirectOutput_SetImage'):
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


img = ctypes.create_string_buffer(b'\x00\x00\xff' * (320 * 240), 320 * 240 * 3)
rouge = ctypes.create_string_buffer(b'\x00\x00\xff' * (320 * 240), 320 * 240 * 3)
vert = ctypes.create_string_buffer(b'\x00\xff\x00' * (320 * 240), 320 * 240 * 3)


def seti(h, pid, buf, label):
    try:
        do.DirectOutput_SetImage(h, pid, 0, 320 * 240 * 3, buf)
        print(f'    SetImage({label}) -> OK')
        return True
    except OSError as e:
        print(f'    SetImage({label}) -> ECHEC {e.winerror & 0xFFFFFFFF:08X}')
        return False


def addp(h, pid, flags):
    try:
        hr = do.DirectOutput_AddPage(h, pid, f'p{pid}', flags)
        print(f'    AddPage({pid}, flags={flags}) -> {hr & 0xFFFFFFFF:08X}')
        return hr == 0
    except OSError as e:
        print(f'    AddPage({pid}, flags={flags}) -> ECHEC {e.winerror & 0xFFFFFFFF:08X}')
        return False


print('Initialize:', hex(do.DirectOutput_Initialize('probe2') & 0xFFFFFFFF))
do.DirectOutput_Enumerate(on_enum, None)
h = found[0]

print('CAS A : id 0 ACTIVE (config historique)')
addp(h, 0, 1)
time.sleep(0.3)
seti(h, 0, rouge, '0')

print('CAS B : id 1 ACTIVE seul (apres RemovePage 0)')
try:
    do.DirectOutput_RemovePage(h, 0)
except OSError:
    pass
time.sleep(0.3)
addp(h, 1, 1)
time.sleep(0.3)
seti(h, 1, rouge, '1')

print('CAS C : ajout id 2 inactif, SetImage sur 1 puis 2')
addp(h, 2, 0)
time.sleep(0.3)
seti(h, 1, rouge, '1 (active)')
seti(h, 2, vert, '2 (inactive)')

print('CAS D : AddPage(2, ACTIVE) — bascule — puis SetImage')
addp(h, 2, 1)
time.sleep(0.3)
seti(h, 2, vert, '2 (nouvelle active)')
seti(h, 1, rouge, '1 (ancienne active)')
