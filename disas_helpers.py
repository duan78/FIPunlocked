"""
disas_helpers.py — Aides internes de StartServer : chargeur de ressource (0x180017fd0),
taille (0x180003f20), writer de payload (0x180006210), lecteur de reponse (0x180006310),
+ dump de la globale 0x180030D20 (nom de ressource).
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


def read_cstr(va, maxlen=120):
    off = va - base
    if off < 0 or off >= len(data):
        return None
    end = data.find(b'\x00', off)
    if end < 0 or end - off > maxlen:
        return None
    return data[off:end].decode('utf-8', 'replace')


def read_wstr(va, maxlen=120):
    off = va - base
    if off < 0 or off + 1 >= len(data):
        return None
    out = []
    while len(out) < maxlen:
        ch = data[off:off + 2]
        if ch == b'\x00\x00' or len(ch) < 2:
            break
        out.append(ch.decode('utf-16-le', 'replace'))
        off += 2
    s = ''.join(out)
    return s if s.isprintable() and len(s) >= 2 else None


# --- la globale 0x180030D20 ---
G = 0x180030D20
print(f'=== globale {G:#x} ===')
print('  hex :', data[G - base:G - base + 48].hex(' '))
print('  strA:', read_cstr(G))
print('  strW:', read_wstr(G))
# et si c'est un pointeur, suivre
import struct
ptr = struct.unpack('<Q', data[G - base:G - base + 8])[0]
if 0 < ptr - base < len(data):
    print(f'  -> pointeur {ptr:#x} : strA={read_cstr(ptr)!r} strW={read_wstr(ptr)!r}')
    print('     hex :', data[ptr - base:ptr - base + 64].hex(' '))

# resources PE ?
print('\n=== .rsrc ===')
if hasattr(pe, 'DIRECTORY_ENTRY_RESOURCE'):
    for rt in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        tname = rt.name if rt.name else f'type {rt.id}'
        for rid in rt.directory.entries:
            for rlang in rid.directory.entries:
                d = rlang.data.struct
                print(f'  type={tname} id={rid.name or rid.id} lang={rlang.id} '
                      f'rva={d.OffsetToData:#x} size={d.Size}')
else:
    print('  (pas de directory resource)')

imports = {}
if hasattr(pe, 'DIRECTORY_ENTRY_IMPORT'):
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        dll = entry.dll.decode()
        for imp in entry.imports:
            if imp.address:
                nm = imp.name.decode() if imp.name else f'ord{imp.ordinal}'
                imports[imp.address] = f'{dll}!{nm}'


def disas(start, title, max_len=250):
    fe = find_func(start)
    end = fe[1] if fe else start + 0x300
    print(f'\n====== {title} [{start:#x}..{end:#x}] ======')
    n = 0
    for insn in md.disasm(data[start - base:end - base], start):
        line = f'  {insn.address:#010x}  {insn.mnemonic:<9} {insn.op_str}'
        if insn.mnemonic in ('call', 'jmp') and insn.operands and insn.operands[0].type == capstone.x86.X86_OP_IMM:
            tgt = insn.operands[0].imm
            tf = find_func(tgt)
            line += f'   ; sub_{tgt:x}' + (f' [{tf[1] - tf[0]:#x} oct]' if tf else '')
        for op in insn.operands:
            if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
                va = insn.address + insn.size + op.mem.disp
                if va in imports:
                    line += f'   ; IAT {imports[va]}'
                else:
                    for rd in (read_cstr, read_wstr):
                        s = rd(va)
                        if s and len(s) > 2 and s.isprintable():
                            line += f'   ; "{s}"'
                            break
        print(line)
        n += 1
        if n >= max_len:
            print('  ... (tronque)')
            break


for addr, name in [(0x180017fd0, 'resource_loader'),
                   (0x180003f20, 'resource_size'),
                   (0x180006210, 'payload_writer'),
                   (0x180006310, 'reply_reader')]:
    disas(addr, name)
