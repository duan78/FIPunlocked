import frida, subprocess, time, sys

def log(*a):
    print(*a, flush=True)
    with open('frida_debug.out', 'a') as f:
        f.write(' '.join(str(x) for x in a) + '\n')

open('frida_debug.out', 'w').close()
subprocess.run(['powershell','-NoProfile','-Command','Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force'], capture_output=True)
time.sleep(2)

JS = r"""
send({t:'boot'});
Interceptor.attach(Module.getGlobalExportByName('NtCreateFile'), {
    onEnter(args) { this.objAttr = args[2]; this.hnd = args[0]; },
    onLeave(retval) {
        if (retval.toInt32() !== 0) return;
        try {
            const namePtr = Memory.readPointer(this.objAttr.add(16));
            if (namePtr.isNull()) return;
            const len = Memory.readU16(namePtr);
            const buf = Memory.readPointer(namePtr.add(8));
            const name = buf.readUtf16String(len/2) || '';
            countNtCreate++;
        if (countNtCreate < 5 || /vid_06a3|f60e9c9c|saitek/i.test(name)) {
                send({t:'OPEN', n: countNtCreate, h: Memory.readPointer(this.hnd).toString(), name: name});
            }
        } catch (e) { send({t:'err', m: '' + e}); }
    }
});
"""

def on_msg(m, d):
    if m['type'] == 'send':
        log('MSG:', str(m['payload'])[:250])
    else:
        log('SCRIPT-ERR:', str(m)[:250])

pid = frida.spawn([r'C:\Program Files\SPAD.neXt\SPAD.neXt.exe'])
log('spawn OK', pid)
s = frida.attach(pid)
sc = s.create_script(JS)
sc.on('message', on_msg)
sc.load()
log('script charge')
frida.resume(pid)
log('SPAD resume, ecoute 45 s')
time.sleep(45)
log('fin')
