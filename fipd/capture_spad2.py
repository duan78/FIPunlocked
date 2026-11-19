"""
capture_spad2.py — Capture COMPLETE de l'init SPAD -> FIP, interface bulk incluse.

Corrige le defaut de sniff_spad.py : la fenetre de 60 s fermait AVANT la phase bulk.
Ici : on capture jusqu'a 5 min APRES le 'Turn device ON', ecritures bulk en hex complet
(<= 4200 o), lectures HID filtrees (bruit 65/s). Sortie : spad_full_session.log
"""
import frida
import os
import subprocess
import threading
import time

SPAD = r'C:\Program Files\SPAD.neXt\SPAD.neXt.exe'
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'spad_full_session.log')
LOG = os.path.expandvars(r'%APPDATA%\SPAD.neXt\logs\spad.log')

JS = r"""
const fipHandles = new Set();
const isBulk = new Map();
let seq = 0;
let hidReads = 0;

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
            if (name.toLowerCase().indexOf('vid_06a3') >= 0) {
                const h = this.hnd.readPointer();
                fipHandles.add(h.toString());
                isBulk.set(h.toString(), name.indexOf('mi_01') >= 0);
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
        let data = this.len <= 4200 ? hx(this.buf, this.len)
                                    : hx(this.buf, 132) + '...[' + this.len + ' tronque]';
        send({t: 'write', bulk: isBulk.get(this.h), h: this.h, seq: seq++,
              len: this.len, data: data, io: retval.toInt32()});
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
        send({t: 'ioctl', bulk: isBulk.get(this.h), h: this.h, seq: seq++,
              code: '0x' + this.ioc.toString(16),
              inl: this.inl, in: this.inl > 0 ? hx(this.inb, this.inl) : '',
              outl: this.outl, out: this.outl > 0 ? hx(this.outb, this.outl) : '',
              io: retval.toInt32()});
    }
});

Interceptor.attach(Module.getGlobalExportByName('NtReadFile'), {
    onEnter(args) {
        if (!isFip(args[0])) return;
        const bulk = isBulk.get(args[0].toString());
        const buf = args[5];
        const len = args[6].toInt32();
        const s = seq++;
        if (bulk) {
            setTimeout(function () {
                send({t: 'readbulk', seq: s, len: len, data: hx(buf, len)});
            }, 300);
        } else if (hidReads < 6) {
            hidReads++;
            setTimeout(function () {
                send({t: 'readhid', seq: s, len: len, data: hx(buf, len)});
            }, 300);
        }
    }
});
"""

lines = []
stats = {'bulk_w': 0, 'ioctl': 0}
done = threading.Event()


def on_message(message, data):
    if message['type'] == 'error':
        lines.append('JS-ERR: ' + message.get('description', '')[:200])
        return
    if message['type'] != 'send':
        return
    p = message['payload']
    t = p['t']
    if t == 'open':
        line = f"OPEN {'BULK' if 'mi_01' in p['name'].lower() else 'HID '} {p['name']}"
    elif t == 'write':
        stats['bulk_w'] += 1 if p['bulk'] else 0
        line = f"W#{p['seq']:04d}{' B' if p['bulk'] else ' H'} len={p['len']:6d} io={p['io']} {p['data'][:264]}"
    elif t == 'ioctl':
        line = f"I#{p['seq']:04d}{' B' if p['bulk'] else ' H'} {p['code']} in={p['inl']}:{p['in'][:60]} out={p['outl']}:{p['out'][:60]} io={p['io']}"
    elif t == 'readbulk':
        line = f"R#{p['seq']:04d} B len={p['len']:6d} {p['data'][:264]}"
    elif t == 'readhid':
        line = f"R#{p['seq']:04d} H len={p['len']:6d} {p['data']}"
    else:
        return
    lines.append(line)
    if t in ('open', 'readbulk') or ' B ' in line[:20]:
        print(line[:160], flush=True)


def main():
    subprocess.run(['powershell', '-NoProfile', '-Command',
                    'Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force'],
                   capture_output=True)
    time.sleep(2)
    pos = os.path.getsize(LOG) if os.path.exists(LOG) else 0

    pid = frida.spawn([SPAD])
    session = frida.attach(pid)
    script = session.create_script(JS)
    script.on('message', on_message)
    script.load()
    frida.resume(pid)
    print('SPAD lance sous Frida. Si possible : DEBRANCHE/REBRANCHE le FIP maintenant.', flush=True)

    # attendre 'Turn device ON' (jusqu'a 8 min)
    t0 = time.time()
    while time.time() - t0 < 480:
        time.sleep(3)
        try:
            sz = os.path.getsize(LOG)
            if sz < pos:
                pos = 0
            with open(LOG, 'rb') as f:
                f.seek(pos)
                fresh = f.read()
            pos += len(fresh)
        except OSError:
            continue
        if b'MZBEAF15DB' in fresh and (b'Turn device ON' in fresh or b'Version fip' in fresh):
            print('>>> init FIP vu, capture etendue 300 s...', flush=True)
            break

    # capture longue apres init
    time.sleep(300)
    try:
        frida.kill(pid)
    except Exception:
        pass

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'\n{len(lines)} lignes, {stats["bulk_w"]} ecritures bulk -> {OUT}', flush=True)


if __name__ == '__main__':
    main()
