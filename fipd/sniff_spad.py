"""
sniff_spad.py — Capture la session complete SPAD -> FIP (interface driver {f60e9c9c...}).

Frida hook NtWriteFile / NtDeviceIoControlFile / NtReadFile / NtCreateFile dans SPAD.neXt,
filtre les handles ouverts sur l'interface FIP, loggue les buffers. Resultat : spad_session.log
"""
import frida
import subprocess
import time
import os
import re
import sys

SPAD = r'C:\Program Files\SPAD.neXt\SPAD.neXt.exe'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'spad_session.log')
LOG = os.path.expandvars(r'%APPDATA%\SPAD.neXt\logs\spad.log')

JS = r"""
const fipHandles = new Set();
const fipBulk = new Map();
let seq = 0;

function hx(ptr, len) {
    try {
        const max = Math.min(len, 96);
        const bytes = ptr.readByteArray(max);
        return Array.from(new Uint8Array(bytes)).map(b => b.toString(16).padStart(2, '0')).join('');
    } catch (e) { return 'ERR'; }
}

Interceptor.attach(Module.getGlobalExportByName('NtCreateFile'), {
    onEnter(args) {
        this.objAttr = args[2];
        this.hnd = args[0];
    },
    onLeave(retval) {
        if (retval.toInt32() !== 0) return;
        try {
            const obj = this.objAttr;
            const namePtr = obj.add(16).readPointer();  // OBJECT_ATTRIBUTES.ObjectName @ +0x10
            if (namePtr.isNull()) return;
            const buf = namePtr.add(8).readPointer();
            const len = namePtr.readU16();
            const name = buf.readUtf16String(len / 2) || '';
            if (name.toLowerCase().indexOf('f60e9c9c') >= 0 || name.toLowerCase().indexOf('vid_06a3') >= 0) {
                const h = this.hnd.readPointer();
                fipHandles.add(h.toString());
                fipBulk.set(h.toString(), name.indexOf('mi_01') >= 0);
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
        this.skip = false;
    },
    onLeave(retval) {
        if (this.skip !== false || !this.len) return;
        send({t: 'write', h: this.h, seq: seq++, len: this.len,
              data: hx(this.buf, this.len), io: retval.toInt32()});
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
    },
    onLeave(retval) {
        if (this.ioc === undefined) return;
        let out = '';
        if (this.outl > 0 && !this.outb.isNull()) out = hx(this.outb, Math.min(this.outl, 64));
        send({t: 'ioctl', h: this.h, seq: seq++, code: '0x' + this.ioc.toString(16),
              inl: this.inl, in: this.inl > 0 && !this.inb.isNull() ? hx(this.inb, Math.min(this.inl, 64)) : '',
              outl: this.outl, out: out, io: retval.toInt32()});
    }
});

Interceptor.attach(Module.getGlobalExportByName('NtReadFile'), {
    onEnter(args) {
        if (!isFip(args[0])) return;
        const len = args[6].toInt32();
        const kind = (args[0].toString().length > 0 && fipBulk.get(args[0].toString())) ? 'BULK' : 'HID';
        send({t: 'read', k: kind, h: args[0].toString(), seq: seq++, len: len});
    }
});
"""

def main():
    subprocess.run(['powershell', '-NoProfile', '-Command',
                    'Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force'],
                   capture_output=True)
    time.sleep(2)
    pos = os.path.getsize(LOG) if os.path.exists(LOG) else 0

    lines = []

    def on_message(message, data):
        if message['type'] == 'error':
            print('JS-ERR:', message.get('description', '')[:120], flush=True)
            return
        if message['type'] != 'send':
            return
        try:
            p = message['payload']
            line = None
            if p['t'] == 'open':
                line = f"OPEN {p['name']}"
            elif p['t'] == 'write':
                line = f"W#{p['seq']:04d} len={p['len']:6d} io={p['io']} {p['data']}"
            elif p['t'] == 'ioctl':
                line = f"I#{p['seq']:04d} {p['code']} in={p['inl']}:{p['in']} out={p['outl']}:{p['out']} io={p['io']}"
            elif p['t'] == 'read':
                line = "R#{:04d} [{}] len={}".format(p['seq'], p.get('k', '?'), p.get('len'))
            if line:
                lines.append(line)
                print(line[:160], flush=True)
        except Exception as e:
            print('fmt-err:', str(e)[:80], flush=True)

    pid = frida.spawn([SPAD])
    session = frida.attach(pid)
    script = session.create_script(JS)
    script.on('message', on_message)
    script.load()
    frida.resume(pid)
    print('SPAD lance sous Frida — rejet des dialogues...', flush=True)

    from pywinauto import Application
    app = None
    t0 = time.time()
    init_seen = False
    while time.time() - t0 < 240:
        time.sleep(2)
        txt = ''
        try:
            with open(LOG, 'rb') as f:
                f.seek(0, 2); sz = f.tell()
                if sz < pos: pos = 0
                f.seek(pos); txt = f.read().decode('utf-8', 'replace')
        except OSError:
            pass
        if 'MZBEAF15DB.Panel|Turn device ON' in txt:
            init_seen = True
            print('Turn device ON vu', flush=True)
            break
        if app is None and time.time() - t0 > 10:
            try:
                app = Application(backend='uia').connect(path='SPAD.neXt.exe', timeout=3)
            except Exception:
                app = None
        if app is not None:
            try:
                for w in app.windows():
                    acted = False
                    for b in w.descendants(control_type='Button'):
                        t = (b.window_text() or '').strip()
                        if re.match(r'^(ok|skip|dismiss.*|close)$', t, re.I) and b.is_enabled():
                            b.invoke(); acted = True; time.sleep(2); break
                    if acted:
                        break
            except Exception:
                app = None

    print(f'\nCapture interactive 60 s — TOURNEZ LES KNOBS (init_seen={init_seen})...', flush=True)
    time.sleep(60)

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'{len(lines)} lignes -> {OUT}', flush=True)

    try:
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        'Get-Process SPAD.neXt -ErrorAction SilentlyContinue | Stop-Process -Force'],
                       capture_output=True)
    except Exception:
        pass

if __name__ == '__main__':
    main()
