"""
probe_init3.py — Initialize DirectOutput.dll AVEC une boucle de messages dans un thread.
Theorie : le manager attend des messages Windows (worker thread a fenetre cachee).
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

user32 = ctypes.WinDLL('user32')
kernel32 = ctypes.WinDLL('kernel32')

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('style', wt.UINT), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON), ('hCursor', wt.HANDLE),
                ('hbrBackground', wt.HBRUSH), ('lpszMenuName', wt.LPCWSTR),
                ('lpszClassName', wt.LPCWSTR), ('hIconSm', wt.HICON)]


@WNDPROC
def wndproc(hwnd, msg, wp, lp):
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


pump_ready = threading.Event()


def pump_thread():
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(wc)
    wc.lpfnWndProc = wndproc
    wc.lpszClassName = 'FipdPump'
    wc.hInstance = kernel32.GetModuleHandleW(None)
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        print(f'RegisterClass err={ctypes.get_last_error()}', flush=True)
    hwnd = user32.CreateWindowExW(0, 'FipdPump', 'fipd', 0, 0, 0, 0, 0, None, None,
                                  wc.hInstance, None)
    print(f'pump: hwnd={hwnd}', flush=True)
    pump_ready.set()
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    print('pump: fin', flush=True)


user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, wt.LPVOID]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

t = threading.Thread(target=pump_thread, daemon=True)
t.start()
pump_ready.wait(5)
time.sleep(0.3)

os.chdir(r'C:\Program Files\Logitech\DirectOutput')
do = ctypes.WinDLL(r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll')
print('DLL chargee, Initialize (15 s max)...', flush=True)
do.DirectOutput_Initialize.restype = ctypes.HRESULT
do.DirectOutput_Initialize.argtypes = [ctypes.c_wchar_p]

res = []


def init_in_thread():
    hr = do.DirectOutput_Initialize('fipd-probe')
    res.append(hr)


th = threading.Thread(target=init_in_thread, daemon=True)
th.start()
th.join(15)
if res:
    print(f'Initialize hr=0x{res[0] & 0xFFFFFFFF:08X}', flush=True)
    time.sleep(3)
    do.DirectOutput_Deinitialize.restype = ctypes.HRESULT
    do.DirectOutput_Deinitialize()
    print('Deinitialize ok — THEORIE POMPE CONFIRMEE', flush=True)
else:
    print('Initialize TOUJOURS bloque', flush=True)
