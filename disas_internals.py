"""
disas_internals.py — Desassemble les implémentations internes appelées par les exports :
  0x180007370 : Create (vrai corps)
  0x180005810 : StartServer (vrai corps)
avec resolution des appels internes directs.
"""
import sys
import pefile
import capstone

PATH = r'C:\Program Files\Logitech\DirectOutput\DirectOutputSaiFip.dll'
pe = pefile.PE(PATH)
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()

md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
md.detail = True

exports = {}
for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
    if exp.name:
        exports[base + exp.address] = exp.name.decode()

funcs = sorted((base + e.struct.BeginAddress, base + e.struct.EndAddress)
               for e in pe.DIRECTORY_ENTRY_EXCEPTION)


def find_func(addr):
    for s, e in funcs:
        if s <= addr < e:
            return s, e
    return None


def read_cstr(va, maxlen=100):
    off = va - base
    if off < 0 or off >= len(data):
        return None
    end = data.find(b'\x00', off)
    if end < 0 or end - off > maxlen:
        return None
    return data[off:end].decode('utf-8', 'replace')


imports = {}
if hasattr(pe, 'DIRECTORY_ENTRY_IMPORT'):
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        dll = entry.dll.decode()
        for imp in entry.imports:
            if imp.address:
                nm = imp.name.decode() if imp.name else f'ord{imp.ordinal}'
                imports[imp.address] = f'{dll}!{nm}'


def disas(start, title, max_len=600):
    fe = find_func(start)
    end = fe[1] if fe else start + 0x300
    print(f'\n====== {title} [{start:#x}..{end:#x}] ======')
    code = data[start - base:end - base]
    n = 0
    for insn in md.disasm(code, start):
        line = f'  {insn.address:#010x}  {insn.mnemonic:<9} {insn.op_str}'
        if insn.mnemonic in ('call', 'jmp') and insn.operands and insn.operands[0].type == capstone.x86.X86_OP_IMM:
            tgt = insn.operands[0].imm
            if tgt in exports:
                line += f'   ; EXPORT {exports[tgt]}'
            else:
                tf = find_func(tgt)
                line += f'   ; sub_{tgt:x}' + (f' (fin {tf[1]:#x})' if tf else '')
        for op in insn.operands:
            if op.type == capstone.x86.X86_OP_MEM:
                m = op.mem
                if m.base in (capstone.x86.X86_REG_RIP, 0) and m.disp:
                    va = insn.address + insn.size + m.disp if m.base == capstone.x86.X86_REG_RIP else base + m.disp
                    if va in imports:
                        line += f'   ; IAT {imports[va]}'
                    else:
                        s = read_cstr(va)
                        if s and len(s) > 2 and s.isprintable() and not s.startswith('!'):
                            line += f'   ; "{s}"'
        print(line)
        n += 1
        if n >= max_len:
            print('  ... (tronque)')
            break


for addr, name in [(0x180007370, 'Create_impl'), (0x180005810, 'StartServer_impl')]:
    disas(addr, name)
