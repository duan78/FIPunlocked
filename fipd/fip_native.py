"""
fip_native.py — Backend natif : appelle DirectOutputSaiFip.dll (exports DirectOutputDriver_*)
depuis Python via ctypes. La DLL gere l'ouverture du device, l'init (StartServer), l'affichage,
les LEDs, la luminosite et les boutons.
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

# la DLL charge ses descripteurs .dat depuis le repertoire courant
os.chdir(r'C:\Program Files\Logitech\DirectOutput')

DLL_PATH = r'C:\Program Files\Logitech\DirectOutput\DirectOutput\..\DirectOutputSaiFip.dll'
DLL_PATH = r'C:\Program Files\Logitech\DirectOutput\DirectOutputSaiFip.dll'

SOFTBUTTONS = {0x01: 'select', 0x02: 'right_cw', 0x04: 'right_ccw', 0x08: 'left_ccw',
               0x10: 'left_cw', 0x20: 'button1', 0x40: 'button2', 0x80: 'button3',
               0x100: 'button4', 0x200: 'button5', 0x400: 'button6'}

SOFTBUTTON_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p)


class FipNative:
    def __init__(self, device_path=None):
        import fip
        if device_path is None:
            paths = fip.find_fip_paths()
            if not paths:
                raise RuntimeError('interface FIP introuvable')
            device_path = paths[0]
        # la DLL attend le chemin SANS le suffixe d'interface GUID ? on teste tel quel d'abord
        self.path = device_path
        self.dll = ctypes.CDLL(DLL_PATH)
        d = self.dll
        for n in ('DirectOutputDriver_Create', 'DirectOutputDriver_StartServer',
                  'DirectOutputDriver_SetImage', 'DirectOutputDriver_SetLed',
                  'DirectOutputDriver_SetString', 'DirectOutputDriver_SetProfile',
                  'DirectOutputDriver_SetNumberOfPages', 'DirectOutputDriver_DefaultPageActivate',
                  'DirectOutputDriver_ClearDisplay', 'DirectOutputDriver_Destroy',
                  'DirectOutputDriver_GetSerialNumber', 'DirectOutputDriver_GetTypeGuid',
                  'DirectOutputDriver_GetInterfaceGuid'):
            getattr(d, n).restype = ctypes.c_long  # HRESULT (32 bits)
        self.handle = ctypes.c_void_p()
        self._cb = SOFTBUTTON_CB(self._on_buttons_raw)
        self.on_buttons = None
        self._last = 0

        # Create(len_en_octets, chemin_utf16, &handle, cb?, ctx?)
        buf = device_path.encode('utf-16-le') + b'\x00\x00'
        hr = d.DirectOutputDriver_Create(len(buf), buf, ctypes.byref(self.handle),
                                         self._cb, None)
        self._check(hr, 'Create')
        if not self.handle:
            raise RuntimeError('Create n a pas retourne de handle')

    def _check(self, hr, what):
        if hr != 0:
            raise RuntimeError(f'{what} -> HRESULT 0x{hr & 0xFFFFFFFF:08X}')

    # ------- callbacks -------
    def _on_buttons_raw(self, hdev, buttons, ctx):
        if self.on_buttons:
            changed = buttons ^ self._last
            self._last = buttons
            names = [n for b, n in SOFTBUTTONS.items() if changed & b]
            try:
                self.on_buttons(buttons, changed, names)
            except Exception:
                pass

    # ------- API -------
    def start_server(self, profile=b'fipd\x00'):
        hr = self.dll.DirectOutputDriver_StartServer(self.handle, len(profile), profile, 0)
        self._check(hr, 'StartServer')
        return True

    def set_profile(self, name):
        b = name if isinstance(name, bytes) else name.encode() + b'\x00'
        return self.dll.DirectOutputDriver_SetProfile(self.handle, len(b), b)

    def set_number_of_pages(self, n):
        return self.dll.DirectOutputDriver_SetNumberOfPages(self.handle, n)

    def activate_default_page(self):
        return self.dll.DirectOutputDriver_DefaultPageActivate(self.handle)

    def set_image(self, rgb, page=0, index=0):
        assert len(rgb) == 320 * 240 * 3
        buf = ctypes.create_string_buffer(rgb, len(rgb))
        return self.dll.DirectOutputDriver_SetImage(self.handle, page, index, len(rgb), buf)

    def set_led(self, page, index, value):
        return self.dll.DirectOutputDriver_SetLed(self.handle, page, index, value)

    def clear_display(self):
        return self.dll.DirectOutputDriver_ClearDisplay(self.handle)

    def serial(self):
        buf = ctypes.create_string_buffer(64)
        hr = self.dll.DirectOutputDriver_GetSerialNumber(self.handle, buf)
        if hr == 0:
            return buf.value.decode('ascii', 'replace')
        return None

    def destroy(self):
        return self.dll.DirectOutputDriver_Destroy(self.handle)


if __name__ == '__main__':
    import fip
    fn = FipNative()
    print('Handle:', fn.handle.value and hex(fn.handle.value))
    print('Serial:', fn.serial())
    print('StartServer:', fn.start_server())
    print('NbPages(1):', fn.set_number_of_pages(1))
    print('ActivatePage:', fn.activate_default_page())
    img = b'\x00\x00\xff' * (320 * 240)
    print('SetImage rouge:', fn.set_image(img, page=0, index=0))
    time.sleep(2)
    for i in range(1, 7):
        print(f'LED {i}:', fn.set_led(0, i, 1))
        time.sleep(0.3)
    time.sleep(2)
    for i in range(1, 7):
        fn.set_led(0, i, 0)
    fn.destroy()
    print('FIN — observez l ecran et les LEDs')
