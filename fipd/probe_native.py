import faulthandler, sys
faulthandler.enable()
print('1: imports...', flush=True)
import ctypes, ctypes.wintypes as wt
import fip
print('2: enum interfaces...', flush=True)
paths = fip.find_fip_paths()
print('   bulk:', paths, flush=True)
print('   hid :', [p[-60:] for p in fip.find_hid_paths()], flush=True)
print('3: chdir + chargement DLL...', flush=True)
import os
os.chdir(r'C:\Program Files\Logitech\DirectOutput')
dll = ctypes.CDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutputSaiFip.dll')
print('   DLL chargee', flush=True)
for n in ('DirectOutputDriver_Create','DirectOutputDriver_GetSerialNumber'):
    getattr(dll, n).restype = ctypes.c_long
print('4: Create...', flush=True)
CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p)
cb = CB(lambda *a: None)
buf = paths[0].encode('utf-16-le') + b'\x00\x00'
h = ctypes.c_void_p()
hr = dll.DirectOutputDriver_Create(len(buf), buf, ctypes.byref(h), cb, None)
print(f'   Create hr=0x{hr & 0xFFFFFFFF:08X} handle={h.value}', flush=True)
print('5: GetSerialNumber...', flush=True)
sb = ctypes.create_string_buffer(64)
hr = dll.DirectOutputDriver_GetSerialNumber(h, sb)
print(f'   hr=0x{hr & 0xFFFFFFFF:08X} serial={sb.value!r}', flush=True)
print('OK', flush=True)
