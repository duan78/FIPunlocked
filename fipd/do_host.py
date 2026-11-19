"""
do_host.py — Hote standard DirectOutput (flux FIPUnlocked) en ctypes.

Initialize -> RegisterDeviceCallback -> Enumerate -> (par device)
RegisterSoftButtonCallback -> AddPage(ACTIVE) -> SetImage(rouge) -> LEDs.
Observe 45 s : les callbacks boutons s'affichent. NE PAS lancer directement :
utilisez capture_do_host.py (Frida).
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

os.chdir(r'C:\Program Files\Logitech\DirectOutput')
do = ctypes.WinDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll')

hr_all = ['DirectOutput_Initialize', 'DirectOutput_Deinitialize',
          'DirectOutput_RegisterDeviceCallback', 'DirectOutput_Enumerate',
          'DirectOutput_RegisterPageCallback', 'DirectOutput_RegisterSoftButtonCallback',
          'DirectOutput_GetDeviceType', 'DirectOutput_GetDeviceInstance',
          'DirectOutput_SetProfile', 'DirectOutput_AddPage', 'DirectOutput_RemovePage',
          'DirectOutput_SetLed', 'DirectOutput_SetString', 'DirectOutput_SetImage',
          'DirectOutput_SetImageFromFile', 'DirectOutput_StartServer', 'DirectOutput_CloseServer',
          'DirectOutput_SaveFile', 'DirectOutput_DisplayFile', 'DirectOutput_DeleteFile']
for n in hr_all:
    getattr(do, n).restype = ctypes.HRESULT

ENUM_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
DEV_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.BOOL, ctypes.c_void_p)
PAGE_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, wt.BOOL, ctypes.c_void_p)
BTN_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p)

VOID_P = ctypes.c_void_p
for n, at in [
        ('DirectOutput_GetDeviceType', [VOID_P, VOID_P]),
        ('DirectOutput_RegisterSoftButtonCallback', [VOID_P, BTN_CB, VOID_P]),
        ('DirectOutput_RegisterPageCallback', [VOID_P, PAGE_CB, VOID_P]),
        ('DirectOutput_AddPage', [VOID_P, wt.DWORD, wt.LPCWSTR, wt.DWORD]),
        ('DirectOutput_RemovePage', [VOID_P, wt.DWORD]),
        ('DirectOutput_SetImage', [VOID_P, wt.DWORD, wt.DWORD, wt.DWORD, VOID_P]),
        ('DirectOutput_SetLed', [VOID_P, wt.DWORD, wt.DWORD, wt.DWORD]),
        ('DirectOutput_StartServer', [VOID_P, wt.DWORD, wt.LPCWSTR, VOID_P, VOID_P]),
        ('DirectOutput_SetProfile', [VOID_P, wt.DWORD, wt.LPCWSTR])]:
    getattr(do, n).argtypes = at

devices = []
t0 = time.time()
log = lambda m: print(f'[{time.time() - t0:6.2f}] {m}', flush=True)


@DEV_CB
def on_dev(h, added, ctx):
    log(f'DEVICE {"AJOUT" if added else "RETRAIT"} h={h}')
    if added:
        devices.append(h)


@ENUM_CB
def on_enum(h, ctx):
    log(f'ENUM h={h}')
    devices.append(h)


@PAGE_CB
def on_page(h, page, active, ctx):
    log(f'PAGE {page} active={bool(active)}')


@BTN_CB
def on_btn(h, buttons, ctx):
    names = []
    for bit, nm in [(0x20, 'S1'), (0x40, 'S2'), (0x80, 'S3'), (0x100, 'S4'),
                    (0x200, 'S5'), (0x400, 'S6'), (0x01, 'SELECT'), (0x02, 'R_CW'),
                    (0x04, 'R_CCW'), (0x08, 'L_CCW'), (0x10, 'L_CW')]:
        if buttons & bit:
            names.append(nm)
    log(f'*** BOUTONS dwButtons={buttons:#05x} {names}')


log('Initialize')
hr = do.DirectOutput_Initialize('fipd-host')
log(f'  hr=0x{hr & 0xFFFFFFFF:08X}')
do.DirectOutput_RegisterDeviceCallback(on_dev, None)
log('Enumerate')
do.DirectOutput_Enumerate(on_enum, None)
log(f'devices: {[hex(d or 0) for d in devices]}')

class GUID(ctypes.Structure):
    _fields_ = [('Data1', wt.DWORD), ('Data2', wt.WORD), ('Data3', wt.WORD),
                ('Data4', wt.BYTE * 8)]

    def __str__(self):
        return '{:08X}-{:04X}-{:04X}-{}-{}'.format(
            self.Data1, self.Data2, self.Data3,
            ''.join(f'{b:02X}' for b in self.Data4[:2]),
            ''.join(f'{b:02X}' for b in self.Data4[2:]))


for h in devices:
    g = GUID()
    hr = do.DirectOutput_GetDeviceType(h, ctypes.byref(g))
    log(f'GetDeviceType hr=0x{hr & 0xFFFFFFFF:08X} guid={g if not hr else "?"}')
    do.DirectOutput_RegisterSoftButtonCallback(h, on_btn, None)
    hr = do.DirectOutput_AddPage(h, 0, 'fipd', 1)   # FLAG_SET_AS_ACTIVE
    log(f'AddPage(0, ACTIVE) hr=0x{hr & 0xFFFFFFFF:08X}')
    img = ctypes.create_string_buffer(b'\x00\x00\xff' * (320 * 240), 320 * 240 * 3)
    hr = do.DirectOutput_SetImage(h, 0, 0, 320 * 240 * 3, img)
    log(f'SetImage rouge hr=0x{hr & 0xFFFFFFFF:08X}')
    for i in range(6):
        try:
            do.DirectOutput_SetLed(h, 0, i, 1)
        except OSError as e:
            log(f'SetLed({i}) E_FAIL ({e.winerror:#x}) — ignore')
            break

log('>>> OBSERVATION 45 s — APPUYEZ SUR LES 6 BOUTONS + TOURNEZ LES KNOBS <<<')
time.sleep(45)
do.DirectOutput_Deinitialize()
log('FIN DO_HOST')
