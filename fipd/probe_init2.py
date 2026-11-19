import ctypes, os, sys, time
print('A: demarrage', flush=True)
os.chdir(r'C:\Program Files\Logitech\DirectOutput')
print('B: chdir ok', flush=True)
do = ctypes.WinDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll')
print('C: DLL chargee', flush=True)
do.DirectOutput_Initialize.restype = ctypes.HRESULT
do.DirectOutput_Initialize.argtypes = [ctypes.c_wchar_p]
print('D: proto ok, appel Initialize...', flush=True)
hr = do.DirectOutput_Initialize('probe')
print(f'E: hr=0x{hr & 0xFFFFFFFF:08X}', flush=True)
