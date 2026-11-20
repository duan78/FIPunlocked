import faulthandler; faulthandler.enable()
import ctypes, os, time
os.chdir(r'C:\Program Files\Logitech\DirectOutput')
do = ctypes.WinDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll')
do.DirectOutput_Initialize.restype = ctypes.HRESULT
do.DirectOutput_Initialize.argtypes = [ctypes.c_wchar_p]
print('appel Initialize(None)...', flush=True)
hr = do.DirectOutput_Initialize(None)
print(f'hr=0x{hr & 0xFFFFFFFF:08X}', flush=True)
time.sleep(2)
print('ok', flush=True)
