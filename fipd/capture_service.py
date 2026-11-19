"""
capture_service.py — Lance DirectOutputService.exe (hote canonique de DirectOutput.dll)
sous Frida et capture tout le trafic FIP + fichiers Logitech ouverts.
Sortie : service_session.log
"""
import frida
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'service_session.log')
SVC = r'C:\Program Files\Logitech\DirectOutput\DirectOutputService.exe'

JS = r"""
const fipHandles = new Set();
const isBulk = new Map();
let seq = 0;

function hx(ptr, len) {
    try {
        const bytes = ptr.readByteArray(Math.min(len, 4200));
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


def main():
    pid = frida.spawn([SVC], cwd=r'C:\Program Files\Logitech\DirectOutput')
    session = frida.attach(pid)
    script = session.create_script(JS)
    script.on('message', on_message)
    script.load()
    frida.resume(pid)
    print('>>> DirectOutputService lance sous Frida (75 s)...', flush=True)
    time.sleep(75)
    try:
        frida.kill(pid)
    except Exception:
        pass
    time.sleep(1)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'{len(lines)} lignes -> {OUT}', flush=True)


if __name__ == '__main__':
    main()
