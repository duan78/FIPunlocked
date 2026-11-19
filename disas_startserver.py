"""
disas_startserver.py — Desassemble DirectOutputDriver_StartServer (et exports voisins)
de DirectOutputSaiFip.dll pour retrouver la signature exacte (cause de l E_INVALIDARG).
"""
import pefile
import capstone

PATH = r'C:\Program Files\Logitech\DirectOutput\DirectOutputSaiFip.dll'
pe = pefile.PE(PATH)
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()

md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
md.detail = True

# table des exports -> VA
exports = {}
for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
    if exp.name:
        exports[base + exp.address] = exp.name.decode()
name_at = {va: n for va, n in exports.items()}

# fonctions depuis .pdata
funcs = sorted((base + e.struct.BeginAddress, base + e.struct.EndAddress)
               for e in pe.DIRECTORY_ENTRY_EXCEPTION)


def find_func(addr):
    for s, e in funcs:
        if s <= addr < e:
            return s, e
    return None


def read_cstr(va, maxlen=80):
    off = va - base
    if off < 0 or off >= len(data):
        return None
    end = data.find(b'\x00', off)
    if end < 0 or end - off > maxlen:
        return None
    try:
        return data[off:end].decode('utf-8', 'replace')
    except Exception:
        return None


def disas(start, end, title, max_len=400):
    print(f'\n====== {title} [{start:#x}..{end:#x}] ======')
    code = data[start - base:end - base]
    n = 0
    for insn in md.disasm(code, start):
        line = f'  {insn.address:#010x}  {insn.mnemonic:<8} {insn.op_str}'
        # annotations : imports (call via IAT), chaines
        if insn.mnemonic in ('call', 'jmp') and insn.operands and insn.operands[0].type == capstone.x86.X86_OP_IMM:
            tgt = insn.operands[0].imm
            if tgt in name_at:
                line += f'   ; -> {name_at[tgt]}'
        for op in insn.operands:
            if op.type == capstone.x86.X86_OP_MEM and op.mem.disp:
                s = read_cstr(base + op.mem.disp)
                if s and len(s) > 3 and s.isprintable():
                    line += f'   ; "{s}"'
        print(line)
        n += 1
        if n >= max_len:
            print('  ... (tronque)')
            break


# 1) StartServer lui-meme
target = next(va for va, n in exports.items() if n == 'DirectOutputDriver_StartServer')
fe = find_func(target) or (target, target + 0x200)
disas(fe[0], fe[1], 'DirectOutputDriver_StartServer', max_len=300)

# 2) pour comparer : Create (qui marche)
target2 = next(va for va, n in exports.items() if n == 'DirectOutputDriver_Create')
fe2 = find_func(target2) or (target2, target2 + 0x200)
disas(fe2[0], fe2[1], 'DirectOutputDriver_Create', max_len=160)

print('\n====== tous les exports ======')
for va, n in sorted(exports.items()):
    print(f'  {va:#x}  {n}')
