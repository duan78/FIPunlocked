"""
capture_svc_elevated.py — A EXECUTER EN ADMIN. Enchaine :
  1. SeDebugPrivilege + attachement Frida au DirectOutputService (SYSTEM)
  2. fenetre 150 s : l'utilisateur POWERCYCLE le FIP -> le service re-init a froid
     -> capture complete (upload serveur embarque inclus)
  3. arret du service (sc stop) pour liberer le device au pilote pur
Sortie : service_init.log
"""
import ctypes
import frida
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'service_init.log')

JS = r"""
const fipHandles = new Set();
const isBulk = new Map();
let seq = 0;

function hx(ptr, len) {
    try {
        const bytes = ptr.readByteArray(Math.min(len, 8192));
        return Array.from(new Uint8Array(bytes)).map(b => b.toString(16).padStart(2, '0')).join('');
    } catch (e) { return 'ERR'; }
}

Interceptor.attach(Module.getGlobalExportByName('NtCreateFile'), {
    onEnter(args) { this.objAttr = args[2]; this.hnd = args[0]; },
    onLeave(retval) {
        if (retval.toInt32() !== 0) return;
        try {
            const namePtr = this.objAttr.add(16).readPointer();
            if (namePtr.isNull()) return;
            const buf = namePtr.add(8).readPointer();
            const len = namePtr.readU16();
            const name = buf.readUtf16String(len / 2) || '';
            const low = name.toLowerCase();
            if (low.indexOf('vid_06a3') >= 0) {
                const h = this.hnd.readPointer();
                fipHandles.add(h.toString());
                isBulk.set(h.toString(), low.indexOf('mi_01') >= 0);
                send({t: 'open', k: low.indexOf('mi_01') >= 0 ? 'BULK' : 'HID', name: name});
            } else if (low.indexOf('ogitech') >= 0 || low.indexOf('irectoutput') >= 0) {
                send({t: 'file', name: name});
            }
        } catch (e) {}
    }
});

function isFip(h) { return fipHandles.has(h.toString()); }

Interceptor.attach(Module.getGlobalExportByName('NtWriteFile'), {
    onEnter(args) {
        if (!isFip(args[0])) return;
        this.buf = args[5];
        this.len = args[6].toInt32();
        this.h = args[0].toString();
        this.go = true;
    },
    onLeave(retval) {
        if (!this.go) return;
        let data = this.len <= 4200 ? hx(this.buf, this.len)
                                    : hx(this.buf, 132) + '...[' + this.len + ' tronque]';
        send({t: 'write', bulk: isBulk.get(this.h), seq: seq++, len: this.len,
              data: data, io: retval.toInt32()});
    }
});

Interceptor.attach(Module.getGlobalExportByName('NtDeviceIoControlFile'), {
    onEnter(args) {
        if (!isFip(args[0])) return;
        this.h = args[0].toString();
        this.ioc = args[5].toInt32() >>> 0;
        this.inb = args[6];
        this.inl = args[7].toInt32();
        this.outb = args[8];
        this.outl = args[9].toInt32();
        this.go = true;
    },
    onLeave(retval) {
        if (!this.go) return;
        send({t: 'ioctl', bulk: isBulk.get(this.h), seq: seq++, code: '0x' + this.ioc.toString(16),
              inl: this.inl, in: this.inl > 0 ? hx(this.inb, this.inl) : '',
              outl: this.outl, out: this.outl > 0 ? hx(this.outb, this.outl) : '',
              io: retval.toInt32()});
    }
});

let hidReads = 0;
Interceptor.attach(Module.getGlobalExportByName('NtReadFile'), {
    onEnter(args) {
        if (!isFip(args[0])) return;
        const bulk = isBulk.get(args[0].toString());
        if (!bulk && hidReads >= 10) return;
        if (!bulk) hidReads++;
        const buf = args[5];
        const len = args[6].toInt32();
        const s = seq++;
        setTimeout(function () {
            send({t: bulk ? 'readbulk' : 'readhid', seq: s, len: len, data: hx(buf, len)});
        }, 300);
    }
});
"""


def enable_sedebug():
    import ctypes.wintypes as wt
    adv = ctypes.WinDLL('advapi32')
    k32 = ctypes.WinDLL('kernel32')
    h = k32.GetCurrentProcess()
    tok = wt.HANDLE()
    adv.OpenProcessToken(h, 0x28, ctypes.byref(tok))   # TOKEN_ADJUST_PRIVILEGES|QUERY
    class LUID(ctypes.Structure):
        _fields_ = [('LowPart', wt.DWORD), ('HighPart', wt.LONG)]
    luid = LUID()
    adv.LookupPrivilegeValueW(None, 'SeDebugPrivilege', ctypes.byref(luid))
    class TP(ctypes.Structure):
        class _1(ctypes.Structure):
            pass
        _fields_ = [('PrivilegeCount', wt.DWORD), ('Luid', LUID), ('Attributes', wt.DWORD)]
    tp = TP()
    tp.PrivilegeCount = 1
    tp.Luid = luid
    tp.Attributes = 2  # SE_PRIVILEGE_ENABLED
    ok = adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
    err = k32.GetLastError()
    print(f'SeDebugPrivilege: adjust={ok} err={err}', flush=True)


def main():
    lines = []

    def on_message(message, data):
        if message['type'] == 'error':
            lines.append('JS-ERR: ' + message.get('description', '')[:200])
            return
        if message['type'] != 'send':
            return
        p = message['payload']
        t = p['t']
        if t == 'open':
            line = f"OPEN {p['k']} {p['name']}"
        elif t == 'file':
            line = f"FILE {p['name']}"
        elif t == 'write':
            line = f"W#{p['seq']:04d}{' B' if p['bulk'] else ' H'} len={p['len']:6d} io={p['io']} {p['data']}"
        elif t == 'ioctl':
            line = f"I#{p['seq']:04d}{' B' if p['bulk'] else ' H'} {p['code']} in={p['inl']}:{p['in'][:80]} out={p['outl']}:{p['out'][:80]} io={p['io']}"
        else:
            line = f"R#{p['seq']:04d}{' B' if t == 'readbulk' else ' H'} len={p['len']:6d} {p['data']}"
        lines.append(line)
        if t in ('open', 'file', 'ioctl') or ' B ' in line[:20]:
            print(line[:170], flush=True)

    enable_sedebug()
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          "(Get-CimInstance Win32_Service -Filter \"Name='DirectOutputService'\").ProcessId"],
                         capture_output=True, text=True).stdout.strip()
    pid = int(out)
    print(f'service pid={pid}', flush=True)
    try:
        session = frida.attach(pid)
    except Exception as e:
        print(f'ATTACH ECHOUE: {e}', flush=True)
        print('on arrete quand meme le service', flush=True)
        subprocess.run(['sc', 'stop', 'DirectOutputService'], capture_output=True)
        with open(OUT, 'w', encoding='utf-8') as f:
            f.write('ATTACH FAILED\n' + str(e))
        time.sleep(3)
        return
    script = session.create_script(JS)
    script.on('message', on_message)
    script.load()
    print('', flush=True)
    print('=' * 62, flush=True)
    print(' HOOKS POSES SUR LE SERVICE.', flush=True)
    print(' >>> DEBRANCHE LE FIP MAINTENANT, ATTENDS 5 s, REBRANCHE-LE <<<', flush=True)
    print('     (fenetre de 150 s)', flush=True)
    print('=' * 62, flush=True)
    for i in range(150, 0, -10):
        print(f'  capture... {i:3d} s restantes', flush=True)
        time.sleep(10)
    try:
        script.unload()
    except Exception:
        pass
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'{len(lines)} lignes -> {OUT}', flush=True)
    print('ARRET DU SERVICE...', flush=True)
    subprocess.run(['sc', 'stop', 'DirectOutputService'], capture_output=True)
    time.sleep(3)
    print('SERVICE ARRETE — tu peux fermer cette fenetre.', flush=True)
    time.sleep(10)


if __name__ == '__main__':
    main()
