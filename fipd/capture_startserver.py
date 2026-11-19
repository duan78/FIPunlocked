"""
capture_startserver.py — Capture COMPLETE de la sequence d'init de la DLL sur le FIP.

Spawn target_init.py sous Frida, hook Nt*File, dump INTEGRALEMENT les ecritures bulk
(le binaire StartServer doit apparaitre en clair) + les reponses de lecture (dump differe
de 350 ms : le target tourne lentement exprès). Sortie : startserver_session.log
"""
import frida
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'startserver_session.log')
PY = r'C:\Python314\python.exe'

JS = r"""
const fipHandles = new Set();
let seq = 0;

function hx(ptr, len) {
    try {
        const bytes = ptr.readByteArray(Math.min(len, 65536));
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
            if (name.toLowerCase().indexOf('vid_06a3') >= 0) {
                const h = this.hnd.readPointer();
                fipHandles.add(h.toString());
                send({t: 'open', h: h.toString(), name: name});
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
        let data;
        if (this.len <= 4200) data = hx(this.buf, this.len);           // complet (header+petits chunks)
        else data = hx(this.buf, 132) + '...[' + this.len + ' tronque]';  // image : entete suffit
        send({t: 'write', h: this.h, seq: seq++, len: this.len, data: data, io: retval.toInt32()});
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
        send({t: 'ioctl', h: this.h, seq: seq++, code: '0x' + this.ioc.toString(16),
              inl: this.inl, in: this.inl > 0 ? hx(this.inb, this.inl) : '',
              outl: this.outl, out: this.outl > 0 ? hx(this.outb, this.outl) : '',
              io: retval.toInt32()});
    }
});

// Lectures : dump DIFFERE (le IO overlapped complete apres le retour de l'appel)
Interceptor.attach(Module.getGlobalExportByName('NtReadFile'), {
    onEnter(args) {
        if (!isFip(args[0])) return;
        const buf = args[5];
        const len = args[6].toInt32();
        const h = args[0].toString();
        const s = seq++;
        setTimeout(function () {
            send({t: 'read', h: h, seq: s, len: len, data: hx(buf, len)});
        }, 350);
    }
});
"""


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
            line = f"OPEN {p['name']}"
        elif t == 'write':
            line = f"W#{p['seq']:04d} len={p['len']:6d} io={p['io']} {p['data']}"
        elif t == 'ioctl':
            line = f"I#{p['seq']:04d} {p['code']} in={p['inl']}:{p['in']} out={p['outl']}:{p['out']} io={p['io']}"
        elif t == 'read':
            line = f"R#{p['seq']:04d} len={p['len']:6d} {p['data']}"
        else:
            return
        lines.append(line)
        print(line[:150], flush=True)

    pid = frida.spawn([PY, os.path.join(HERE, 'target_init.py')],
                      cwd=HERE)
    session = frida.attach(pid)
    script = session.create_script(JS)
    script.on('message', on_message)
    script.load()
    frida.resume(pid)
    print('>>> cible lancee sous Frida, capture en cours...', flush=True)

    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(2)
        try:
            proc = session.enumerate_processes()  # juste pour garder la session vivante
        except Exception:
            break
        if 'TARGET DONE' in '\n'.join(lines[-5:]):
            break
    # laisser fuiter les derniers dumps differes
    time.sleep(2)
    try:
        frida.kill(pid)
    except Exception:
        pass

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'\n{len(lines)} lignes -> {OUT}', flush=True)


if __name__ == '__main__':
    main()
