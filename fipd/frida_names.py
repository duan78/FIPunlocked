import frida, subprocess, time, os

open('frida_names.out', 'w').close()
subprocess.run(['powershell','-NoProfile','-Command','Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force'], capture_output=True)
time.sleep(1)

JS = r"""
send('boot');
let n = 0;
Interceptor.attach(Module.getGlobalExportByName('NtCreateFile'), {
    onEnter(args) { this.objAttr = args[2]; this.hnd = args[0]; },
    onLeave(retval) {
        if (retval.toInt32() !== 0) return;
        n++;
        if (n > 30) return;
        try {
            const namePtr = this.objAttr.add(16).readPointer();
            let name = '(null)';
            if (!namePtr.isNull()) {
                const len = namePtr.readU16();
                const buf = namePtr.add(8).readPointer();
                name = buf.readUtf16String(len/2) || '(vide)';
            }
            send('OPEN#' + n + ' ' + name.slice(0, 90));
        } catch (e) { send('OPEN#' + n + ' ERR ' + e); }
    }
});
"""

def log(msg):
    print(msg, flush=True)
    with open('frida_names.out', 'a') as f:
        f.write(msg + '\n')

def on_msg(m, d):
    if m['type'] == 'send':
        log(str(m['payload'])[:120])
    else:
        log('ERR ' + str(m)[:150])

pid = frida.spawn([r'C:\Program Files\SPAD.neXt\SPAD.neXt.exe'])
s = frida.attach(pid)
sc = s.create_script(JS)
sc.on('message', on_msg)
sc.load()
frida.resume(pid)
log('SPAD demarre')
time.sleep(40)
subprocess.run(['powershell','-NoProfile','-Command','Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force'], capture_output=True)
log('fin')
