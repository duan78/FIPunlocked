"""
capture_service2.py — S'attache au DirectOutputService (doit tourner) avec Frida,
puis lance do_host.py (client pipe). Le service fait l'init device dans SON process :
tout est capture. Sortie : service_session2.log
"""
import frida
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'service_session2.log')

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
            } else if (low.indexOf('ogitech') >= 0 || low.indexOf('irectoutput') >= 0
                       || (low.indexOf('\\dat') >= 0) || (low.indexOf('.dat') >= 0)) {
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
        print('JS-ERR:', message.get('description', '')[:150], flush=True)
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
    # PID du service (SYSTEM)
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          "(Get-CimInstance Win32_Service -Filter \"Name='DirectOutputService'\").ProcessId"],
                         capture_output=True, text=True).stdout.strip()
    if not out or out == '':
        print('service DirectOutputService introuvable — lancez service_setup.ps1 en eleve', flush=True)
        sys.exit(1)
    pid = int(out)
    print(f'service pid={pid}, attachement Frida...', flush=True)

    session = frida.attach(pid)
    script = session.create_script(JS)
    script.on('message', on_message)
    script.load()
    print('hooks poses. Lancement du client do_host.py (son affichage suit)...', flush=True)

    client = subprocess.Popen([sys.executable, os.path.join(HERE, 'do_host.py')],
                              cwd=HERE)
    t0 = time.time()
    while time.time() - t0 < 70 and client.poll() is None:
        time.sleep(1)
    if client.poll() is None:
        client.kill()
    try:
        script.unload()
    except Exception:
        pass
    time.sleep(1)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'{len(lines)} lignes -> {OUT}', flush=True)


if __name__ == '__main__':
    main()
