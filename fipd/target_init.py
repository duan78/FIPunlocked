"""
target_init.py — Cible Frida : sequence DLL complete Create -> StartServer -> pages -> image.
Tourne lentement (0.4 s entre appels) pour laisser le hook dumper les buffers de lecture.
NE PAS lancer directement : utilisez capture_startserver.py.
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

import fip

os.chdir(r'C:\Program Files\Logitech\DirectOutput')
dll = ctypes.CDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutputSaiFip.dll')
for n in ('DirectOutputDriver_Create', 'DirectOutputDriver_StartServer',
          'DirectOutputDriver_SetImage', 'DirectOutputDriver_SetLed',
          'DirectOutputDriver_SetNumberOfPages', 'DirectOutputDriver_DefaultPageActivate',
          'DirectOutputDriver_ClearDisplay', 'DirectOutputDriver_Destroy'):
    getattr(dll, n).restype = ctypes.c_long

step = lambda m: (print(m, flush=True), time.sleep(0.4))

CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p)
cb = CB(lambda *a: None)

paths = fip.find_fip_paths()
buf = paths[0].encode('utf-16-le') + b'\x00\x00'
h = ctypes.c_void_p()

step('== Create ==')
print('hr Create =', dll.DirectOutputDriver_Create(len(buf), buf, ctypes.byref(h), cb, None),
      'handle =', h.value, flush=True)

step('== StartServer ==')
prof = b'fipd\x00'
print('hr StartServer =', dll.DirectOutputDriver_StartServer(h, len(prof), prof, 0), flush=True)

step('== SetNumberOfPages(1) ==')
print('hr =', dll.DirectOutputDriver_SetNumberOfPages(h, 1), flush=True)

step('== DefaultPageActivate ==')
print('hr =', dll.DirectOutputDriver_DefaultPageActivate(h), flush=True)

step('== SetImage rouge ==')
img = b'\x00\x00\xff' * (320 * 240)
b = ctypes.create_string_buffer(img, len(img))
print('hr =', dll.DirectOutputDriver_SetImage(h, 0, 0, len(img), b), flush=True)

time.sleep(1.5)
step('== ClearDisplay ==')
print('hr =', dll.DirectOutputDriver_ClearDisplay(h), flush=True)

step('== Destroy ==')
print('hr =', dll.DirectOutputDriver_Destroy(h), flush=True)
print('TARGET DONE', flush=True)
