import re

d = open(r'C:\Program Files\Logitech\DirectOutput\DirectOutputService.exe', 'rb').read()
# chaines ASCII et UTF-16 exploitables
asc = set(m.group().decode() for m in re.finditer(rb'[\x20-\x7e]{5,}', d))
uni = set(m.group().decode('utf-16-le') for m in re.finditer(rb'(?:[\x20-\x7e]\x00){5,}', d))
kws = ['service', 'console', 'run', 'debug', 'usage', 'install', 'pipe', 'event', 'mutex',
       'DirectOutput', 'StartServiceCtrlDispatcher', '-']
for s in sorted(asc | uni):
    low = s.lower()
    if any(k.lower() in low for k in kws) and len(s) < 90:
        print(repr(s))
