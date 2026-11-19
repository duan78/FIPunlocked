"""
target2.py — Create() du plugin + callbacks + DefaultPageActivate, SANS service.
Si les boutons tirent : le backend DLL direct suffit (et l'init pur = sequence Create).
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

import fip

os.chdir(r'C:\Program Files\Logitech\DirectOutput')
dll = ctypes.CDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutputSaiFip.dll')
for n in ('DirectOutputDriver_Create', 'DirectOutputDriver_Destroy',
          'DirectOutputDriver_SetImage', 'DirectOutputDriver_SetNumberOfPages',
          'DirectOutputDriver_DefaultPageActivate',
          'DirectOutputDriver_RegisterSoftButtonCallback'):
    getattr(dll, n).restype = ctypes.c_long

CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p)
NAMES = [(0x20, 'S1'), (0x40, 'S2'), (0x80, 'S3'), (0x100, 'S4'),
         (0x200, 'S5'), (0x400, 'S6'), (0x01, 'SELECT'), (0x02, 'R_CW'),
         (0x04, 'R_CCW'), (0x08, 'L_CCW'), (0x10, 'L_CW')]
evts = []


@CB
def on_btn(h, buttons, ctx):
    names = [n for b, n in NAMES if buttons & b]
    print(f'*** BOUTONS dwButtons={buttons:#05x} {names}', flush=True)
    evts.append(buttons)


t0 = time.time()
log = lambda m: print(f'[{time.time()-t0:6.2f}] {m}', flush=True)

paths = fip.find_fip_paths()
buf = paths[0].encode('utf-16-le') + b'\x00\x00'
h = ctypes.c_void_p()
log('Create (callback passe en arg4, comme le service le ferait)...')
hr = dll.DirectOutputDriver_Create(len(buf), buf, ctypes.byref(h), on_btn, None)
log(f'  hr=0x{hr & 0xFFFFFFFF:08X} handle={h.value}')
if hr != 0:
    raise SystemExit('Create echoue')

log('RegisterSoftButtonCallback...')
hr = dll.DirectOutputDriver_RegisterSoftButtonCallback(h, on_btn, None)
log(f'  hr=0x{hr & 0xFFFFFFFF:08X}')

log('SetNumberOfPages(1)...')
hr = dll.DirectOutputDriver_SetNumberOfPages(h, 1)
log(f'  hr=0x{hr & 0xFFFFFFFF:08X}')

log('DefaultPageActivate(1)...')
hr = dll.DirectOutputDriver_DefaultPageActivate(h)
log(f'  hr=0x{hr & 0xFFFFFFFF:08X}')

FORCE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


@FORCE_CB
def on_force(h):
    log('  (DefaultPageForceActivate callback)')


log('DefaultPageRegisterForceActivateCallback...')
dll.DirectOutputDriver_DefaultPageRegisterForceActivateCallback.argtypes = [
    ctypes.c_void_p, FORCE_CB, ctypes.c_void_p]
hr = dll.DirectOutputDriver_DefaultPageRegisterForceActivateCallback(h, on_force, None)
log(f'  hr=0x{hr & 0xFFFFFFFF:08X}')

log('SetImage rouge...')
img = ctypes.create_string_buffer(b'\x00\x00\xff' * (320 * 240), 320 * 240 * 3)
hr = dll.DirectOutputDriver_SetImage(h, 0, 0, 320 * 240 * 3, img)
log(f'  hr=0x{hr & 0xFFFFFFFF:08X}')

log('>>> 45 s : APPUYEZ BOUTONS + KNOBS (keepalive actif) <<<')
t0 = time.time()
while time.time() - t0 < 45:
    time.sleep(3)
    dll.DirectOutputDriver_SetImage(h, 0, 0, 320 * 240 * 3, img)   # keepalive
dll.DirectOutputDriver_Destroy(h)
log(f'FIN — {len(evts)} evenements boutons')
