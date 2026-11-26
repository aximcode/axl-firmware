"""X64 code built for baseline x86-64: no instruction a later level adds.

edk2's GCC X64 flags name no -march, so a compiler whose default is a
later level (x86-64-v3: Ubuntu's amd64v3 gcc, AlmaLinux 10's) builds firmware that faults
(#UD) on an older CPU, or under TCG with a CPU model that lacks the
extension. build.sh pins -march=x86-64 (DESIGN §4.24); this is the check
that the build's code holds to it.

Every X64 module in the code FD (edk2's build report, as licenses.py
reads it) is disassembled from its build's ELF (`DEBUG/<name>.debug`, the
.text GenFw turns into the module's PE) by the host's objdump, and each
instruction inside a function symbol (STT_FUNC, with its size) is held
against the instructions x86-64-v2, -v3 and -v4 add (psABI's levels),
and those of extensions in no level (AES-NI, SHA, ...): EXTENSIONS
below, and any VEX or EVEX instruction (the AVX family and AVX-512). Only function symbols are read because edk2's linker script
puts .rodata in .text: bytes of data decode as instructions too. What
lies outside them is hand-written assembly (NASM, GAS), which -march does
not touch and which edk2 runs only when CPUID says the CPU has it.
"""

from __future__ import annotations

import bisect
import re
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from axlfw import licenses as lic_mod

# The instructions each extension of x86-64-v2 and -v3 adds that a
# compiler emits, by mnemonic as objdump prints them (Intel syntax). The
# levels are the x86-64 psABI's: v2 is CMPXCHG16B, LAHF-SAHF, POPCNT,
# SSE3, SSE4.1, SSE4.2 and SSSE3; v3 adds AVX, AVX2, BMI1, BMI2, F16C,
# FMA, LZCNT, MOVBE and OSXSAVE; v4 AVX-512 (F, BW, CD, DQ, VL). AVX,
# AVX2, FMA, F16C and AVX-512 are every VEX or EVEX instruction
# (_vector). OSXSAVE is the OS's XSAVE support, not an instruction a
# compiler emits: XSAVE and XGETBV come only from hand-written code,
# after a CPUID check.
EXTENSIONS: dict[str, tuple[str, str]] = {}
for _ext, _level, _names in (
    ("CMPXCHG16B", "v2", "cmpxchg16b"),
    ("LAHF-SAHF", "v2", "lahf sahf"),
    ("POPCNT", "v2", "popcnt"),
    ("SSE3", "v2", "addsubpd addsubps haddpd haddps hsubpd hsubps lddqu movddup movshdup movsldup fisttp"),
    (
        "SSSE3",
        "v2",
        "pabsb pabsw pabsd palignr phaddw phaddd phaddsw phsubw phsubd phsubsw pmaddubsw pmulhrsw"
        " pshufb psignb psignw psignd",
    ),
    (
        "SSE4.1",
        "v2",
        "blendpd blendps blendvpd blendvps dppd dpps extractps insertps movntdqa mpsadbw packusdw"
        " pblendvb pblendw pcmpeqq pextrb pextrd pextrq phminposuw pinsrb pinsrd pinsrq pmaxsb pmaxsd"
        " pmaxud pmaxuw pminsb pminsd pminud pminuw pmovsxbw pmovsxbd pmovsxbq pmovsxwd pmovsxwq"
        " pmovsxdq pmovzxbw pmovzxbd pmovzxbq pmovzxwd pmovzxwq pmovzxdq pmuldq pmulld ptest"
        " roundpd roundps roundsd roundss",
    ),
    ("SSE4.2", "v2", "crc32 pcmpestri pcmpestrm pcmpistri pcmpistrm pcmpgtq"),
    ("BMI1", "v3", "andn bextr blsi blsmsk blsr tzcnt"),
    ("BMI2", "v3", "bzhi mulx pdep pext rorx sarx shlx shrx"),
    ("LZCNT", "v3", "lzcnt"),
    ("MOVBE", "v3", "movbe"),
    # In no level: extensions a CPU may lack whatever its level. A compiler
    # emits them only when asked (-maes, -msha, ...), which the makefile
    # route refuses too; edk2 uses them only in hand-written code, after a
    # CPUID check.
    (
        "AES-NI",
        "no level",
        "aesenc aesenclast aesdec aesdeclast aesimc aeskeygenassist",
    ),
    ("PCLMULQDQ", "no level", "pclmulqdq"),
    ("SHA", "no level", "sha1rnds4 sha1nexte sha1msg1 sha1msg2 sha256rnds2 sha256msg1 sha256msg2"),
    ("ADX", "no level", "adcx adox"),
    ("GFNI", "no level", "gf2p8affineqb gf2p8affineinvqb gf2p8mulb"),
    ("RDSEED", "no level", "rdseed"),
):
    for _name in _names.split():
        EXTENSIONS[_name] = (_ext, _level)
VECTOR = ("AVX, AVX2, FMA, F16C or AVX-512 (VEX/EVEX)", "v3")
MASK = ("AVX-512 mask registers", "v4")
# Mnemonics that begin with v and are not VEX: VMX, SVM, SEV-ES's VMGEXIT
# and the segment checks, all baseline or CPUID-gated system instructions.
NOT_VECTOR = frozenset(
    "verr verw vmcall vmclear vmfunc vmgexit vmlaunch vmload vmmcall vmptrld vmptrst vmread"
    " vmresume vmrun vmsave vmwrite vmxoff vmxon".split()
)
_MASK_OP = re.compile(r"^k(?:add|and|andn|mov|not|or|ortest|shiftl|shiftr|test|unpck|xnor|xor)[bwdq]{1,2}$")
# Instruction prefixes objdump prints before the mnemonic.
_PREFIXES = frozenset(
    "lock rep repz repe repnz repne data16 data32 addr16 addr32 notrack bnd xacquire xrelease"
    " cs ds es fs gs ss".split()
)
_REX = re.compile(r"^rex(?:\.[WRXB]+)?$")
# objdump's pseudo-prefixes for an encoding choice ({vex} for AVX-VNNI and
# the like, {evex}, {vex3}), before a v mnemonic: skipped to reach it.
_PSEUDO = re.compile(r"^\{(vex|vex2|vex3|evex)\}$")
_LINE = re.compile(r"^\s*([0-9a-f]+):\t(.*)$")

_SHT_SYMTAB = 2
_STT_FUNC = 2


class ElfError(Exception):
    """The file is no 64-bit little-endian ELF, or its symbol table is unreadable."""


@dataclass(frozen=True)
class Hit:
    """One instruction beyond baseline x86-64."""

    address: int
    mnemonic: str
    extension: str
    level: str
    function: str


@dataclass
class Scan:
    """What one module's scan read and found."""

    functions: int = 0
    instructions: int = 0
    hits: list[Hit] = field(default_factory=lambda: [])


def classify(mnemonic: str) -> tuple[str, str] | None:
    """The extension and level a mnemonic needs, or None for baseline x86-64."""
    m = mnemonic.lower()
    if m in EXTENSIONS:
        return EXTENSIONS[m]
    if _MASK_OP.match(m):
        return MASK
    if m.startswith("v") and m not in NOT_VECTOR:
        return VECTOR
    return None


def mnemonic(text: str) -> str:
    """The mnemonic of one objdump instruction (its prefixes skipped)."""
    for token in text.split():
        if token in _PREFIXES or _REX.match(token) or _PSEUDO.match(token):
            continue
        return token
    return ""


def instruction(text: str) -> tuple[str, tuple[str, str] | None]:
    """One objdump instruction's mnemonic, and the extension and level it needs.

    Beyond classify: pextrw to memory is SSE4.1's (to a register, SSE2's).
    """
    name = mnemonic(text)
    if name.lower() == "pextrw" and "PTR [" in text.split(",", 1)[0]:
        return name, EXTENSIONS["pextrb"]
    return name, classify(name)


def functions(data: bytes) -> list[tuple[int, int, str]]:
    """Each function symbol with a size, (start, end, name), sorted by start."""
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        msg = "not a 64-bit little-endian ELF file"
        raise ElfError(msg)
    out: list[tuple[int, int, str]] = []
    try:
        shoff: int = struct.unpack_from("<Q", data, 0x28)[0]
        shentsize, shnum = cast("tuple[int, int]", struct.unpack_from("<HH", data, 0x3A))
        # Each section header's (sh_type, sh_offset, sh_size, sh_link).
        sections: list[tuple[int, int, int, int]] = []
        for i in range(shnum):
            h = struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
            sections.append((h[1], h[4], h[5], h[6]))
        for kind, offset, length, link in sections:
            if kind != _SHT_SYMTAB:
                continue
            strtab = sections[link][1]
            for off in range(offset, offset + length, 24):
                name_off, info, _other, _shndx, value, size = cast(
                    "tuple[int, int, int, int, int, int]", struct.unpack_from("<IBBHQQ", data, off)
                )
                if info & 0xF != _STT_FUNC or size == 0:
                    continue
                start = strtab + name_off
                name = data[start : data.index(b"\0", start)].decode(errors="replace")
                out.append((value, value + size, name))
    except (struct.error, IndexError, ValueError) as err:
        # A header, symbol or name past the file's end, or a link to no section.
        msg = f"an unreadable section or symbol table ({len(data)} bytes)"
        raise ElfError(msg) from err
    if not out:
        msg = "no function symbols (a stripped file?)"
        raise ElfError(msg)
    # A function symbol inside another (a GAS label typed @function) would
    # end the outer one's range early: overlapping ranges are merged, under
    # the outer name.
    merged: list[tuple[int, int, str]] = []
    for start_, end, name in sorted(out):
        if merged and start_ < merged[-1][1]:
            last = merged[-1]
            merged[-1] = (last[0], max(last[1], end), last[2])
        else:
            merged.append((start_, end, name))
    return merged


def scan_text(disassembly: str, funcs: list[tuple[int, int, str]]) -> Scan:
    """The instructions beyond baseline in objdump's output, inside funcs.

    disassembly is `objdump -d -M intel --no-show-raw-insn -w`'s output.
    """
    starts = [f[0] for f in funcs]
    scan = Scan(functions=len(funcs))
    for line in disassembly.splitlines():
        m = _LINE.match(line)
        if m is None:
            continue
        addr = int(m.group(1), 16)
        i = bisect.bisect_right(starts, addr) - 1
        if i < 0 or addr >= funcs[i][1]:
            continue
        scan.instructions += 1
        name, found = instruction(m.group(2))
        if found:
            scan.hits.append(Hit(addr, name, found[0], found[1], funcs[i][2]))
    return scan


def scan_elf(path: Path, objdump: str = "objdump") -> Scan:
    """One ELF file's .text, disassembled by objdump and scanned."""
    funcs = functions(path.read_bytes())
    out = subprocess.run(
        [objdump, "-d", "-M", "intel", "--no-show-raw-insn", "-w", "-j", ".text", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or out.stderr.strip():
        msg = f"{objdump} failed (status {out.returncode}): {out.stderr.strip()}"
        raise ElfError(msg)
    scan = scan_text(out.stdout, funcs)
    if scan.instructions == 0:
        msg = f"{objdump}'s output has no instruction inside a function"
        raise ElfError(msg)
    return scan


def summary(hits: list[Hit]) -> str:
    """The hits by extension, each with its count and first instruction."""
    by: dict[str, list[Hit]] = {}
    for h in hits:
        by.setdefault(f"{h.extension} ({h.level})", []).append(h)
    return "; ".join(
        f"{ext}: {len(hs)}, first {hs[0].mnemonic} at {hs[0].address:#x} in {hs[0].function}"
        for ext, hs in sorted(by.items())
    )


_CC_FLAGS = re.compile(r"^CC_FLAGS = (.*)$", re.MULTILINE)
_MODULE_NAME = re.compile(r"^MODULE_NAME = (\S+)$", re.MULTILINE)
BASELINE = "-march=x86-64"


# gcc's options that enable an instruction-set extension on top of -march.
_ISA_FLAG = re.compile(
    r"(?<!\S)-m(?:sse3|sse4[0-9a-z.]*|ssse3|avx[0-9a-z_]*|fma4?|f16c|bmi2?|lzcnt|popcnt|movbe|abm|tbm|xop|cx16|sahf"
    r"|aes|vaes|pclmul|vpclmulqdq|sha|sha512|adx|gfni|rdseed|xsave[a-z]*|amx[0-9a-z_-]*)(?!\S)"
)


def _march(makefile: Path) -> tuple[str, list[str]]:
    """A GNUmakefile's C flags: the last -march (or ''), and any ISA enables."""
    cc = _CC_FLAGS.search(makefile.read_text(errors="replace"))
    flags = cc.group(1) if cc else ""
    marches = re.findall(r"(?<!\S)-march=\S+", flags)
    return (marches[-1] if marches else ""), _ISA_FLAG.findall(flags)


def check_baseline(
    report: lic_mod.Report, fd: str, arch_build: Path, roots: list[Path], objdump: str = "objdump"
) -> tuple[dict[str, int], list[str]]:
    """Every module of an X64 FD compiled with -march=x86-64, and its code baseline.

    Two routes. The flags: each module's GNUmakefile, and each library's
    it links (OUTPUT/static_library_files.lst), compiles C with BASELINE
    as its last -march. The code: the module's ELF holds no instruction
    beyond baseline. A module with no ELF must have compiled no object
    (OVMF's ResetVector, NASM straight to a binary). Returns the counts
    (modules scanned, modules without code, libraries, functions,
    instructions) and the problems.
    """
    dirs, problems = lic_mod.module_build_dirs(report, fd, arch_build, roots)
    counts = {"modules": 0, "without_code": 0, "libraries": 0, "functions": 0, "instructions": 0}
    seen: set[Path] = set()

    def flags(what: str, build_dir: Path) -> None:
        makefile = build_dir / "GNUmakefile"
        if not makefile.is_file():
            problems.append(f"{what}: {makefile} missing, so its flags cannot be read")
            return
        march, enables = _march(makefile)
        if march != BASELINE:
            problems.append(f"{what}: {makefile} compiles with {march or 'no -march'}, not {BASELINE}")
        if enables:
            problems.append(f"{what}: {makefile} enables extensions beyond {BASELINE}: {' '.join(enables)}")

    for name, mod in dirs:
        text = (mod / "GNUmakefile").read_text(errors="replace")
        base = _MODULE_NAME.search(text)
        elf = mod / "DEBUG" / f"{base.group(1) if base else name}.debug"
        if not elf.is_file():
            if any((mod / "OUTPUT").rglob("*.obj")):
                problems.append(f"{name}: {elf} missing, though the module compiled objects")
            else:
                counts["without_code"] += 1
            continue
        flags(name, mod)
        libs, more = lic_mod.library_outputs(name, mod)
        problems += more
        for out in libs:
            # The list names the module's own .lib too: its flags are read above.
            if out.parent != mod and out.parent not in seen:
                seen.add(out.parent)
                counts["libraries"] += 1
                flags(f"{name}'s library", out.parent)
        try:
            scan = scan_elf(elf, objdump)
        except (OSError, ElfError) as err:
            problems.append(f"{name}: {elf}: {err}")
            continue
        counts["modules"] += 1
        counts["functions"] += scan.functions
        counts["instructions"] += scan.instructions
        if scan.hits:
            problems.append(f"{name}: {len(scan.hits)} instructions beyond x86-64 in {elf}: {summary(scan.hits)}")
    if counts["modules"] == 0:
        problems.append(f"FD {fd}: no module scanned")
    return counts, problems
