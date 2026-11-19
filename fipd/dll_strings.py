import re

d = open(r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll', 'rb').read()
pats = {
    'pipes': rb'\\\\\.\\pipe\\[\w.$-]+',
    'exe': rb'DirectOutput\w*\.exe',
    'dlls': rb'[Dd]irect\w*\.dll',
    'events/mutexs': rb'(?:Global|Local)\\[\w{}.$-]+',
    'service': rb'\w*[Ss]ervice\w*',
}
for k, p in pats.items():
    found = sorted(set(m.group().decode('ascii', 'replace') for m in re.finditer(p, d)))
    print(f'== {k}: {found[:14]}')

# sections importees (dependances statiques)
import pefile
pe = pefile.PE(r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll')
if hasattr(pe, 'DIRECTORY_ENTRY_IMPORT'):
    print('== imports statiques:', [e.dll.decode() for e in pe.DIRECTORY_ENTRY_IMPORT])
