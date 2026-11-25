"""Which licence files each image needs, from the build's own report.

edk2's `build -y <file> -Y LIBRARY -Y FLASH` writes a report that lists,
per firmware device (FD), the modules in each of its firmware volumes,
nested (compressed) volumes included, and, per module, the library
instances linked into it. A third-party part (an edk2 submodule: OpenSSL,
Mbed TLS, Brotli, libfdt, ...) is in an image when a module in one of its
FD's volumes, or a library linked into that module, lists a source file
inside the submodule in its INF's [Sources] for the arch.

So each image's licence list is edk2's own licence plus every file of
every part so found (docs/RELEASE-FORMAT.md, `images.<dir>.licenses`).
`chosen` is false only for the licence a dual-licensed part is not taken
under (DUAL); the check (check_image_licenses) refuses a list that names a
file the release lacks, misses a part, edk2's licence or a choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

EDK2_LICENSE = "LICENSES/edk2-License.txt"

# Dual-licensed parts: each licence file of the choice, and whether the
# release takes the part under it. A part's other licence files (libtl's
# LICENSE.md, which states the choice) are not part of the choice.
DUAL: dict[str, dict[str, bool]] = {
    "MdePkg/Library/BaseFdtLib/libfdt": {"BSD-2-Clause": True, "GPL": False},
    "MdePkg/Library/ArmTransferListLib/libtl": {"LICENSE-MIT.md": True, "LICENSE-GPL-2.0-or-later.md": False},
}

# The FD each shipped code image is, per arch (the FDF's [FD.<name>]).
CODE_FD = {"x64": "OVMF_CODE", "aarch64": "QEMU_EFI"}
# The arch names edk2 uses in INF section names.
EDK2_ARCH = {"x64": "X64", "aarch64": "AARCH64"}
# Entries of a volume that are neither a module nor a file: the a-priori
# files and OVMF's ACPI table storage (BuildReport.py names them so).
NOT_MODULES = frozenset({"PEI Apriori", "DXE Apriori", "ACPI table storage"})

_SECTION_START = re.compile(r"^>={10,}<$")
_SUBSECTION_START = re.compile(r"^>-{10,}<$")
_SUBSECTION_END = re.compile(r"^<-{10,}>$")
_SEPARATOR = re.compile(r"^-{10,}$")
_ENTRY = re.compile(r"^0x[0-9A-Fa-f]+ (.*)$")
_MODULE_ENTRY = re.compile(r"^(\S+) \((.+\.inf)\)$")
_MACRO = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
# The [Sources] files `build` compiles into objects (headers and other
# files only feed them): a part counts by these alone, as the objects'
# .deps route sees it.
COMPILED = frozenset({".c", ".cc", ".cpp", ".s", ".asm", ".nasm", ".nasmb"})


class ReportError(Exception):
    """The build report is missing, cut short or not as BuildReport.py writes it."""


@dataclass
class Volume:
    """A firmware volume of an FD in the report: its name and its entries."""

    name: str
    entries: list[str] = field(default_factory=lambda: [])


@dataclass
class Report:
    """What the licence lists need from edk2's build report."""

    # FD name -> its volumes (each region's, then its nested ones).
    fds: dict[str, list[Volume]] = field(default_factory=lambda: {})
    # Module INF path, as the report writes it -> the library INFs linked
    # into it, as written; a module built twice (two FILE_GUIDs) has both
    # its lists here.
    modules: dict[str, list[str]] = field(default_factory=lambda: {})
    content: str = ""


def _lines(text: str) -> list[str]:
    return [line.rstrip("\r") for line in text.split("\n")]


def parse_report(text: str) -> Report:
    """Parse a report of `build -Y LIBRARY -Y FLASH`.

    BuildReport.py wraps lines at 120 columns, at a space or a slash: an
    FV entry goes on until its next entry or its subsection's end, a
    library path until its "{class ...}" line, and a module's INF path is
    one line or continues with a "/".
    """
    lines = _lines(text)
    report = Report()
    m = next((re.match(r"^Report Content:\s+(.*)$", x) for x in lines if x.startswith("Report Content:")), None)
    if m is None:
        msg = "no 'Report Content:' line: not an edk2 build report"
        raise ReportError(msg)
    report.content = m.group(1).strip()
    kinds = {k.strip() for k in report.content.split(",")}
    if not {"LIBRARY", "FLASH"} <= kinds:
        msg = f"the report holds {report.content}, not LIBRARY and FLASH"
        raise ReportError(msg)
    fd: str | None = None
    volume: Volume | None = None
    in_entries = False
    # An FV subsection of an FD: BuildReport.py writes its "Fv Name:" and
    # entries only when it can read the volume's .Fv.txt (else it warns).
    fv_section = False
    module: str | None = None
    in_libraries = False
    lib_parts: list[str] = []
    in_class = False
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if _SECTION_START.match(line):
            fd = volume = module = None
            in_entries = in_libraries = in_class = fv_section = False
            continue
        if line.startswith("FD Name:"):
            fd = line.split(":", 1)[1].strip()
            report.fds.setdefault(fd, [])
            continue
        if fd is not None:
            if line == "Type:               FV":
                fv_section = True
                continue
            if line.startswith("Fv Name:"):
                name = line.split(":", 1)[1].strip().split(" (", 1)[0]
                volume = Volume(name)
                report.fds[fd].append(volume)
                in_entries = False
                continue
            if volume is not None and _SEPARATOR.match(line):
                in_entries = True
                continue
            if _SUBSECTION_END.match(line) or _SUBSECTION_START.match(line):
                if fv_section and (volume is None or not volume.entries):
                    what = "no 'Fv Name:'" if volume is None else f"volume {volume.name} with no entry"
                    msg = f"FD {fd}: an FV subsection with {what}: the report could not read the volume"
                    raise ReportError(msg)
                in_entries = fv_section = False
                volume = None
                continue
            if in_entries and volume is not None:
                e = _ENTRY.match(line)
                if e:
                    volume.entries.append(e.group(1))
                elif volume.entries:
                    volume.entries[-1] += line
                continue
            continue
        if line.startswith("Module INF Path:"):
            module = line.split(":", 1)[1].strip()
            while i < len(lines) and lines[i].startswith("/"):
                module += lines[i]
                i += 1
            report.modules.setdefault(module, [])
            continue
        if module is not None:
            if line == "Library" and i < len(lines) and _SEPARATOR.match(lines[i]):
                in_libraries = True
                lib_parts = []
                i += 1
                continue
            if in_libraries:
                if _SUBSECTION_END.match(line):
                    in_libraries = in_class = False
                    if lib_parts:
                        msg = f"{module}: a library path with no class line: {''.join(lib_parts)}"
                        raise ReportError(msg)
                    continue
                if in_class:
                    in_class = not line.rstrip().endswith("}")
                    continue
                if line.startswith("{"):
                    if not lib_parts:
                        msg = f"{module}: a library class line with no path: {line}"
                        raise ReportError(msg)
                    report.modules[module].append("".join(lib_parts))
                    lib_parts = []
                    in_class = not line.rstrip().endswith("}")
                    continue
                lib_parts.append(line)
    if not report.fds:
        msg = "no firmware device: the report has no FLASH section"
        raise ReportError(msg)
    if not report.modules:
        msg = "no module section"
        raise ReportError(msg)
    return report


def _relative(path: str, roots: list[Path]) -> str | None:
    """path relative to the first root (PACKAGES_PATH order) it is under.

    A relative path is already so; an absolute one under no root is None.
    """
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    for root in roots:
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            continue
    return None


def _resolve(rel: str, roots: list[Path]) -> Path | None:
    """The file at rel in the first root that has it, as `build` finds it."""
    for root in roots:
        if (root / rel).is_file():
            return root / rel
    return None


def inf_sources(inf: Path, arch: str) -> list[str]:
    """The [Sources] entries an INF compiles for an edk2 arch with GCC.

    Relative to the INF's directory, with its [Defines] DEFINEs expanded.
    The sections read are [Sources], [Sources.common] and
    [Sources.<arch>]; an entry for another tool family (`| MSFT`) is left
    out. A macro left unexpanded is a ValueError: a guess would hide a part.
    """
    defines: dict[str, str] = {}
    sources: list[str] = []
    section: list[str] = []
    for raw in inf.read_text(errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            names = [n.strip().lower() for n in line.strip("[]").split(",")]
            section = names
            continue
        if section == ["defines"]:
            m = re.match(r"^DEFINE\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if m:
                defines[m.group(1)] = m.group(2).strip()
            continue
        wanted = {"sources", "sources.common", f"sources.{arch.lower()}"}
        if not any(n in wanted for n in section):
            continue
        # <file> | <family> | <tag> | <toolcode> | <feature flag>: only the
        # family is read. A feature flag is not evaluated, so an entry it
        # turns off still counts (the cross-check then fails loudly).
        fields = [f.strip() for f in line.split("|")]
        family = fields[1].upper() if len(fields) > 1 else ""
        if family and family not in {"GCC", "*"}:
            continue
        name = fields[0]
        entry = name
        for _ in range(8):
            expanded = _MACRO.sub(lambda m: defines.get(m.group(1), m.group(0)), entry)
            if expanded == entry:
                break
            entry = expanded
        left = _MACRO.search(entry)
        if left:
            msg = f"{inf}: [Sources] entry {name!r}: no DEFINE for $({left.group(1)})"
            raise ValueError(msg)
        sources.append(entry)
    return sources


def parts_of(inf_rel: str, roots: list[Path], submodules: list[str], arch: str) -> set[str]:
    """The submodules an INF (relative to a package root) compiles sources from."""
    inf = _resolve(inf_rel, roots)
    if inf is None:
        msg = f"{inf_rel}: in no package root"
        raise ValueError(msg)
    base = Path(inf_rel).parent
    found: set[str] = set()
    for entry in inf_sources(inf, arch):
        path = (base / entry).as_posix()
        # An entry may climb (../); normalise without touching the disk.
        parts: list[str] = []
        for piece in path.split("/"):
            if piece == "..":
                if parts:
                    parts.pop()
            elif piece not in ("", "."):
                parts.append(piece)
        norm = "/".join(parts)
        if Path(norm).suffix.lower() not in COMPILED:
            continue
        for sub in submodules:
            if norm.startswith(f"{sub}/"):
                found.add(sub)
    return found


def image_parts(
    report: Report, fd: str, roots: list[Path], submodules: list[str], arch: str
) -> tuple[set[str], list[str]]:
    """The third-party parts compiled into the modules of one FD's volumes.

    Every volume of the FD counts, nested ones included (a compressed DXE
    volume inside FVMAIN_COMPACT). Returns the parts and the problems: an
    FD the report lacks, an entry that is no module, nested volume or
    known file, or a module with no section of its own.
    """
    problems: list[str] = []
    volumes = report.fds.get(fd)
    if not volumes:
        return set(), [f"the build report has no FD {fd}, or no volume in it"]
    names = {v.name for v in volumes}
    found: set[str] = set()
    cache: dict[str, set[str]] = {}

    def of(inf_rel: str) -> set[str]:
        if inf_rel not in cache:
            cache[inf_rel] = parts_of(inf_rel, roots, submodules, arch)
        return cache[inf_rel]

    for v in volumes:
        for entry in v.entries:
            if entry in names or entry in NOT_MODULES:
                continue
            m = _MODULE_ENTRY.match(entry)
            if m is None:
                rel = _relative(entry, roots) if entry.startswith("/") else None
                if rel is None:
                    problems.append(f"FD {fd}, volume {v.name}: cannot tell what {entry!r} is")
                    continue
                # A file placed whole (FDF FILE ... SECTION RAW = <path>).
                found |= {s for s in submodules if rel.startswith(f"{s}/")}
                continue
            rel = _relative(m.group(2), roots)
            if rel is None or rel not in report.modules:
                problems.append(f"FD {fd}, volume {v.name}: {m.group(1)} ({m.group(2)}) has no module section")
                continue
            try:
                found |= of(rel)
                for lib in report.modules[rel]:
                    lib_rel = _relative(lib, roots)
                    if lib_rel is None:
                        problems.append(f"{rel}: library {lib} is under no package root")
                        continue
                    found |= of(lib_rel)
            except (OSError, ValueError) as err:
                problems.append(str(err))
    return found, problems


def _module_entries(report: Report, fd: str) -> list[tuple[str, str]]:
    """Each (name, INF path as written) of the modules in an FD's volumes."""
    out: list[tuple[str, str]] = []
    for v in report.fds.get(fd, []):
        for entry in v.entries:
            m = _MODULE_ENTRY.match(entry)
            if m:
                out.append((m.group(1), m.group(2)))
    return out


def _compiled_source(deps: Path) -> str | None:
    """The source a gcc -MMD dependency file says its object was compiled from."""
    text = deps.read_text(errors="replace")
    _, colon, rest = text.partition(":")
    if not colon:
        return None
    first = rest.replace("\\\n", " ").split()
    return first[0] if first else None


def compiled_parts(
    report: Report, fd: str, arch_build: Path, roots: list[Path], submodules: list[str]
) -> tuple[set[str], list[str]]:
    """The parts whose sources were compiled into objects an FD's modules link.

    A cross-check of image_parts by another route, the build's own output:
    each module's build directory (arch_build/<INF dir>/<INF name>, or a
    FILE_GUID's, whose GNUmakefile names the same MODULE_FILE), its
    OUTPUT/static_library_files.lst (the libraries linked), and in its own
    and each library's OUTPUT, every object's .deps (gcc -MMD), whose first
    prerequisite is the source compiled. Neither the report's library
    lists nor the INFs' [Sources] are read.
    """
    found: set[str] = set()
    problems: list[str] = []
    seen_out: dict[Path, set[str]] = {}

    def of_output(out: Path) -> set[str]:
        if out not in seen_out:
            parts: set[str] = set()
            if not out.is_dir():
                problems.append(f"{out}: no such directory")
            for obj in out.rglob("*.obj"):
                if not obj.with_name(f"{obj.name}.deps").is_file():
                    problems.append(f"{obj}: no .deps, so its source is unknown")
            for deps in out.rglob("*.deps"):
                source = _compiled_source(deps)
                rel = _relative(source, roots) if source else None
                if rel is not None:
                    parts |= {s for s in submodules if rel.startswith(f"{s}/")}
            seen_out[out] = parts
        return seen_out[out]

    for name, inf in _module_entries(report, fd):
        rel = _relative(inf, roots)
        if rel is None:
            problems.append(f"{name} ({inf}) is under no package root")
            continue
        base = arch_build / Path(rel).parent
        dirs = [
            d
            for d in sorted(base.glob("*/GNUmakefile"))
            if re.search(rf"^MODULE_FILE = {re.escape(Path(rel).name)}$", d.read_text(errors="replace"), re.M)
        ]
        if not dirs:
            problems.append(f"{name}: no build directory for {rel} in {arch_build}")
            continue
        for mk in dirs:
            lst = mk.parent / "OUTPUT" / "static_library_files.lst"
            outs = [mk.parent / "OUTPUT"]
            if lst.is_file():
                outs += [Path(x.strip()).parent for x in lst.read_text().splitlines() if x.strip()]
            elif any((mk.parent / "OUTPUT").rglob("*.obj")):
                # A module of objects links libraries; only one built
                # otherwise (OVMF's ResetVector, NASM to a binary) has none.
                problems.append(f"{name}: {lst} missing, so its libraries are unknown")
            for out in outs:
                found |= of_output(out)
    return found, problems


def licence_dir(sub: str) -> str:
    """A part's directory under LICENSES/: its path with / as _."""
    return f"LICENSES/{sub.replace('/', '_')}"


def missing_texts(parts: set[str], top: list[str]) -> list[str]:
    """The parts that have no licence file among top, said."""
    return [
        f"{sub} is in the image, but the release has no licence file of it"
        for sub in sorted(parts)
        if not any(t.startswith(f"{licence_dir(sub)}/") for t in top)
    ]


def image_licenses(parts: set[str], top: list[str]) -> list[dict[str, object]]:
    """An image's licence list: edk2's, then each part's files from top.

    `chosen` is false only for the licence DUAL says a part is not taken
    under. A part with no file in top adds nothing (missing_texts says so).
    """
    out: list[dict[str, object]] = [{"path": EDK2_LICENSE, "chosen": True}]
    for sub in sorted(parts):
        prefix = f"{licence_dir(sub)}/"
        choice = DUAL.get(sub, {})
        for path in top:
            if path.startswith(prefix):
                name = path[len(prefix):]
                out.append({"path": path, "chosen": choice.get(name, True)})
    return out


def check_image_licenses(
    where: str, listed: object, top: list[str], release_dir: Path, parts: set[str] | None
) -> list[str]:
    """The licence-list property of one image (docs/PROPERTIES.md).

    listed is `images.<dir>.licenses` as read. Each entry is exactly
    {"path": str, "chosen": bool}, once; its path one of the top-level
    `licenses` and a file under release_dir/LICENSES; edk2's licence is
    there, chosen; each dual-licensed part listed has exactly one chosen
    licence of its choice, DUAL's, and every other file is chosen. With
    parts (at assembly, those the build's objects were compiled from:
    compiled_parts), every file of each part is listed, and no other.
    """
    if not isinstance(listed, list):
        return [f"{where}: licenses is {listed!r}, not a list"]
    problems: list[str] = []
    seen: dict[str, bool] = {}
    for raw in cast("list[object]", listed):
        entry = cast("dict[object, object]", raw) if isinstance(raw, dict) else None
        if entry is None or sorted(map(str, entry)) != ["chosen", "path"] or len(entry) != 2:
            problems.append(f"{where}: licence entry {raw!r} is not {{path, chosen}}")
            continue
        path = entry.get("path")
        chosen = entry.get("chosen")
        if not isinstance(path, str) or not isinstance(chosen, bool):
            problems.append(f"{where}: licence entry {entry!r}: path not a string, or chosen not a bool")
            continue
        if path in seen:
            problems.append(f"{where}: {path} listed twice")
            continue
        seen[path] = chosen
        if path not in top:
            problems.append(f"{where}: {path} is not among the top-level licenses")
        if not path.startswith("LICENSES/") or ".." in Path(path).parts or not (release_dir / path).is_file():
            problems.append(f"{where}: {path} is not a file in LICENSES/")
    order = list(seen)
    if order != sorted(order, key=lambda x: (x != EDK2_LICENSE, x)):
        problems.append(f"{where}: not in order (edk2's licence first, then by path): {order}")
    if seen.get(EDK2_LICENSE) is not True:
        problems.append(f"{where}: edk2's licence {EDK2_LICENSE} is not listed, chosen")
    for sub, choice in DUAL.items():
        prefix = f"{licence_dir(sub)}/"
        mine = {p[len(prefix):]: c for p, c in seen.items() if p.startswith(prefix)}
        if not mine:
            continue
        picked = sorted(n for n in choice if mine.get(n) is True)
        if len(picked) != 1 or picked != sorted(n for n, c in choice.items() if c):
            problems.append(
                f"{where}: {sub} is dual-licensed: {sorted(choice)} must be listed with exactly "
                f"{sorted(n for n, c in choice.items() if c)} chosen; chosen: {picked}"
            )
        for n in choice:
            if n not in mine:
                problems.append(f"{where}: {sub}: {prefix}{n} is not listed")
    for p, c in seen.items():
        sub_name = next((s for s in DUAL if p.startswith(f"{licence_dir(s)}/")), None)
        if not c and (sub_name is None or p[len(licence_dir(sub_name)) + 1:] not in DUAL[sub_name]):
            problems.append(f"{where}: {p} is not one licence of a choice, so it must be chosen")
    if parts is not None:
        want = {str(e["path"]) for e in image_licenses(parts, top)}
        for p in sorted(want - set(seen)):
            problems.append(f"{where}: {p} is not listed, but its part is compiled into the image")
        for p in sorted(set(seen) - want):
            problems.append(f"{where}: {p} is listed, but its part is not in the image")
        problems += [f"{where}: {x}" for x in missing_texts(parts, top)]
    return problems
