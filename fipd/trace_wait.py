"""
trace_wait.py — Spawn probe_init2.py sous Frida et trace les attentes/synchronisations
pour localiser le blocage de DirectOutput_Initialize (backtrace + noms d'objets).
"""
import frida
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = r'C:\Python314\python.exe'

JS = r"""
function bt(ctx) {
    try {
        return Thread.backtrace(ctx, Backtracer.ACCURATE)
            .slice(0, 12)
            .map(DebugSymbol.fromAddress)
            .map(s => (s.module ? s.module.name + '!' + s.name + '+' + s.offset.toString(16) : s.address.toString()))
            .join('\n    ');
    } catch (e) { return 'bt-err ' + e; }
}

['WaitForSingleObject', 'WaitForSingleObjectEx'].forEach(function (n) {
    const a = Module.getGlobalExportByName(n);
    if (a) Interceptor.attach(a, {
        onEnter(args) {
            if (this.seen) return;
            this.seen = true;
            const t = args[1].toInt32();
            send({t: 'wait', api: n, ms: t, h: args[0].toString(), bt: bt(this.context)});
        }
    });
});
['MsgWaitForMultipleObjects', 'MsgWaitForMultipleObjectsEx', 'CoWaitForMultipleHandles',
 'WaitForMultipleObjects', 'WaitForMultipleObjectsEx', 'SleepEx'].forEach(function (n) {
    const a = Module.findGlobalExportByName(n);
    if (a) Interceptor.attach(a, {
        onEnter(args) {
            send({t: 'wait', api: n, bt: bt(this.context)});
        }
    });
});
['CreateMutexW', 'CreateEventW', 'OpenMutexW', 'CreateWaitableTimerW'].forEach(function (n) {
    const a = Module.findGlobalExportByName(n);
    if (a) Interceptor.attach(a, {
        onEnter(args) {
            let nm = '';
            try { nm = args[2].readUtf16String() || ''; } catch (e) {}
            send({t: 'create', api: n, name: nm, bt: bt(this.context)});
        }
    });
});
// le retour de Initialize nous interesse aussi
const doi = Module.findExportByName('DirectOutput.dll', 'DirectOutput_Initialize');
if (doi) {
    Interceptor.attach(doi, {
        onEnter() { send({t: 'msg', s: 'Initialize ENTREE'}); },
        onLeave(r) { send({t: 'msg', s: 'Initialize RETOUR hr=' + r.toInt32()}); }
    });
}
"""


def main():
    lines = []

    def on_message(message, data):
        if message['type'] == 'error':
            print('JS-ERR:', message.get('description', '')[:150], flush=True)
            return
        p = message.get('payload') or {}
        if p.get('t') == 'msg':
            print('**', p['s'], flush=True)
            lines.append('** ' + p['s'])
        elif p.get('t') == 'wait':
            line = f"WAIT {p['api']} ms={p.get('ms')}\n    {p['bt']}"
            print(line, flush=True)
            lines.append(line)
        elif p.get('t') == 'create':
            line = f"CREATE {p['api']} {p['name']!r}\n    {p['bt']}"
            print(line, flush=True)
            lines.append(line)

    pid = frida.spawn([PY, os.path.join(HERE, 'probe_init2.py')], cwd=HERE)
    s = frida.attach(pid)
    sc = s.create_script(JS)
    sc.on('message', on_message)
    sc.load()
    frida.resume(pid)
    time.sleep(20)
    try:
        frida.kill(pid)
    except Exception:
        pass
    with open(os.path.join(HERE, 'trace_wait.log'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'{len(lines)} entrees -> trace_wait.log', flush=True)


if __name__ == '__main__':
    main()
