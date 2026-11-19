"""
find_server_app.py — Cherche le binaire 'application serveur' du FIP :
n'importe quel fichier de taille 1 Ko..1 Mo dans les zones Logitech/SPAD/fip134,
en excluant les formats connus (PE, .NET, jpg, xml...). Un server app FIP est un
binaire brut (souvent commence par un petit header, pas MZ).
"""
import os
import struct

ROOTS = [
    r'C:\Program Files\Logitech',
    r'C:\Program Files (x86)\Logitech',
    r'C:\Program Files\SPAD.neXt',
    r'C:\ProgramData\Logitech', r'C:\ProgramData\SPAD.neXt',
    os.path.expandvars(r'%APPDATA%\SPAD.neXt'),
    os.path.expandvars(r'%ALLUSERSPROFILE%\Logitech'),
]
WS = r'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78'
ROOTS += [os.path.join(WS, 'fip134'), os.path.join(WS, 'fip_pkg'),
          os.path.join(WS, 'fipunlocked_src')]
EXT_BAD = {'.dll', '.exe', '.sys', '.jpg', '.jpeg', '.png', '.xml', '.config', '.log',
           '.txt', '.pdf', '.chm', '.htm', '.html', '.zip', '.msi', '.cat', '.inf',
           '.dat', '.ttf', '.ico', '.bmp', '.cur', '.ani', '.lic', '.manifest', '.xaml'}


def kind(data):
    if data[:2] == b'MZ':
        return 'PE'
    if data[:1] == b'{' or data[:1] == b'[' or data[:5] == b'<?xml':
        return 'text'
    if len(data) > 40 and data[6:8] in (b'JF',) :
        return 'jpg'
    return '?'


hits = 0
for root in ROOTS:
    if not os.path.isdir(root):
        continue
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', '__pycache__')]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            p = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if not (300 <= sz <= 2_000_000) or ext in EXT_BAD:
                continue
            try:
                with open(p, 'rb') as f:
                    head = f.read(64)
            except OSError:
                continue
            k = kind(head)
            if k == '?':
                hits += 1
                print(f'{sz:8d}  {p}   head={head[:24].hex(" ")}')
                if hits > 60:
                    raise SystemExit
print(f'-- {hits} candidats')
