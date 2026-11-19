"""
disas_activate.py — Desassemble les impl de DefaultPageActivate et
RegisterSoftButtonCallback (thunks 0x18000a480 / 0x180009b10) pour extraire
la sequence wire exacte qui active les evenements HID.
"""
import pefile
import capstone

PATH = r'C:\Program Files\Logitech\DirectOutput\DirectOutputSaiFip.dll'
pe = pefile.PE(PATH)
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()

md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
md.detail = True

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


def read_cstr(va, maxlen=90):
    off = va - base
    if off < 0 or off >= len(data):
        return None
    end = data.find(b'\x00', off)
    if end < 0 or end - off > maxlen:
        return None
    return data[off:end].decode('utf-8', 'replace')


def disas(start, title, max_len=220):
    fe = find_func(start)
    end = fe[1] if fe else start + 0x300
    print(f'\n====== {title} [{start:#x}..{end:#x}] ======')
    n = 0
    for insn in md.disasm(data[start - base:end - base], start):
        line = f'  {insn.address:#010x}  {insn.mnemonic:<9} {insn.op_str}'
        if insn.mnemonic in ('call', 'jmp') and insn.operands and insn.operands[0].type == capstone.x86.X86_OP_IMM:
            tgt = insn.operands[0].imm
            tf = find_func(tgt)
            line += f'   ; sub_{tgt:x}' + (f' [{tf[1]-tf[0]:#x}o]' if tf else '')
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


# thunks -> vraies impl
for thunk, name in [(0x18000a480, 'DefaultPageActivate'),
                    (0x180009b10, 'RegisterSoftButtonCallback'),
                    (0x180009bb0, 'SetProfile'),
                    (0x18000a550, 'SetNumberOfPages')]:
    code = data[thunk - base:thunk - base + 64]
    print(f'\n### thunk {name} ({thunk:#x}):')
    for insn in md.disasm(code, thunk):
        print(f'  {insn.address:#010x}  {insn.mnemonic:<9} {insn.op_str}')
        if insn.mnemonic == 'jmp' and insn.operands and insn.operands[0].type == capstone.x86.X86_OP_IMM:
            tgt = insn.operands[0].imm
            if tgt != thunk:
                disas(tgt, f'{name}_impl')
        if insn.mnemonic in ('ret', 'int3'):
            break
