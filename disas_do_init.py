"""
disas_do_init.py — Desassemble DirectOutput_Initialize de DirectOutput.dll (le manager)
pour identifier le blocage (attente sync, fenetre, SCM, enumerateur...).
"""
import pefile
import capstone

PATH = r'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll'
pe = pefile.PE(PATH)
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()

md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
md.detail = True

exports = {}
for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
    if exp.name:
        exports[base + exp.address] = exp.name.decode()
print('exports:', [n for n in exports.values() if 'Init' in n or 'Enumerate' in n][:10])

funcs = sorted((base + e.struct.BeginAddress, base + e.struct.EndAddress)
               for e in pe.DIRECTORY_ENTRY_EXCEPTION)


def find_func(addr):
    for s, e in funcs:
        if s <= addr < e:
            return s, e
    return None


imports = {}
if hasattr(pe, 'DIRECTORY_ENTRY_IMPORT'):
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        dll = entry.dll.decode()
        for imp in entry.imports:
            if imp.address:
                nm = imp.name.decode() if imp.name else f'ord{imp.ordinal}'
                imports[imp.address] = f'{dll}!{nm}'


def read_cstr(va, maxlen=100):
    off = va - base
    if off < 0 or off >= len(data):
        return None
    end = data.find(b'\x00', off)
    if end < 0 or end - off > maxlen:
        return None
    return data[off:end].decode('utf-8', 'replace')


def disas(start, title, max_len=400, depth=0):
    fe = find_func(start)
    end = fe[1] if fe else start + 0x400
    print(f'\n====== {title} [{start:#x}..{end:#x}] ======')
    n = 0
    for insn in md.disasm(data[start - base:end - base], start):
        line = f'  {insn.address:#010x}  {insn.mnemonic:<9} {insn.op_str}'
        if insn.mnemonic in ('call', 'jmp') and insn.operands and insn.operands[0].type == capstone.x86.X86_OP_IMM:
            tgt = insn.operands[0].imm
            if tgt in exports:
                line += f'   ; EXPORT {exports[tgt]}'
                n += 1
                print(line)
                continue
            tf = find_func(tgt)
            if tf and depth < 1 and (tf[1] - tf[0]) < 0x400:
                n += 1
                print(line + '   ; >>>')
                disas(tgt, f'sub_{tgt:x}', max_len=200, depth=depth + 1)
                continue
            line += f'   ; sub_{tgt:x}'
        for op in insn.operands:
            if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
                va = insn.address + insn.size + op.mem.disp
                if va in imports:
                    line += f'   ; IAT {imports[va]}'
                else:
                    s = read_cstr(va)
                    if s and len(s) > 3 and s.isprintable():
                        line += f'   ; "{s}"'
        print(line)
        n += 1
        if n >= max_len:
            print('  ... (tronque)')
            break


target = next(va for va, n in exports.items() if n == 'DirectOutput_Initialize')
disas(target, 'DirectOutput_Initialize')
