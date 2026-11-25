"""A second reading of an image's FFS files, by edk2's own VolInfo.

The FV walker (fv.py) reads images with uefi_firmware. This module reads
them again with VolInfo, from the build's own BaseTools, and the release
requires the two sets of file GUIDs to be equal (docs/DESIGN.md §4.23).

VolInfo decodes one volume per run, at --offset, and the GUID-defined
(LZMA) sections inside it only through the tools named in the
GuidedSectionTools.txt beside the file it reads, which edk2's build writes
into each FV directory; the tools themselves (LzmaCompress) must be on
PATH. Its output is text for people, so the parser here is strict: every
line must be one it knows, the files it names must add up to the totals it
states, and an exit status other than 0 or anything on stderr is a
failure, never an empty or partial set.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

# EFI_FIRMWARE_VOLUME_HEADER: FvLength (UINT64) at 32, the signature at 40,
# HeaderLength (UINT16) at 48.
_FV_LENGTH = 32
_SIGNATURE = b"_FVH"
_SIGNATURE_OFFSET = 40
_HEADER_LENGTH = 48
# Pad files (EFI_FV_FILETYPE_FFS_PAD) are named so; fv.py drops the same.
FREE_SPACE = "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"
TIMEOUT = 300

_G = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_H = r"0x[0-9A-Fa-f]+"
# Every line VolInfo writes for the images built here, each whole.
_KNOWN = [
    re.compile(p)
    for p in (
        r"",
        r"VolInfo Version \S+ Build .*",
        r"=+",
        r"-+",
        r"Signature:\s+_FVH \([0-9A-Fa-f]+\)",
        r"Attributes:\s+(0x)?[0-9A-Fa-f]+",
        r"EFI_FVB2_[A-Z0-9_]+",
        r"(Header Length|Revision|Number of Blocks|Block Length|Total Volume Size):\s+" + _H,
        rf"File System ID:\s+{_G}",
        r"(File Offset|File Length|File Attributes|File State|Size|Build Number|DataOffset):\s+" + _H,
        r"EFI_FILE_[A-Z_]+",
        r"File Type:\s+" + _H + r"\s+EFI_FV_FILETYPE_[A-Z0-9_]+",
        r"Type:\s+EFI_SECTION_[A-Z0-9_]+",
        r"(Version )?String:\s.*",
        rf"(Guid|SectionDefinitionGuid):\s+{_G}",
        _G,
        r"(PUSH|AND|OR|NOT|TRUE|FALSE|BEFORE|AFTER|SOR|END|END DEPEX)",
        r"(PEI|DXE) APRIORI FILE:",
        r"/-+ (Firmware Volume|Encapsulation) section start -+\\",
        r"\\-+ (Firmware Volume|Encapsulation) section end -+/",
    )
]
_FILE_NAME = re.compile(rf"File Name:\s+({_G})")
# The decompressor VolInfo runs (LzmaCompress -d) prints this to the same
# stdout, unbuffered, so it lands inside whatever line VolInfo's buffer
# ended in ("=====Decoding\n====="): it is cut out before parsing.
_CHATTER = "Decoding\n"
# VolInfo ignores its decompressor's exit status (the system() call in
# VolInfo.c), so a failed decode shows only as an empty encapsulation and
# no "Decoding": each LZMA GUID-defined section must have its "Decoding".
LZMA_GUID = "EE4E5898-3914-4259-9D6E-DC7BD79403CF"
_LZMA_SECTION = re.compile(rf"SectionDefinitionGuid:\s+{LZMA_GUID}", re.IGNORECASE)
_ENCAPSULATION_START = "/------------ Encapsulation section start -----------------\\"
_ENCAPSULATION_END = "\\------------ Encapsulation section end -------------------/"
_TOTAL = re.compile(r"There are a total of (\d+) files in (this|the child) FV")


class VolInfoError(Exception):
    """VolInfo failed, or wrote output this parser does not know."""


def parse(text: str) -> set[str]:
    """The FFS file GUIDs (upper case, pad files dropped) in one run's output.

    Strict: an unknown line, no "files in this FV" total, or File Name
    lines that do not add up to the stated totals is a VolInfoError.
    """
    names: list[str] = []
    totals: list[int] = []
    this_fv = 0
    decoded = text.count(_CHATTER)
    lzma = 0
    previous = ""
    for n, raw in enumerate(text.replace(_CHATTER, "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if previous == _ENCAPSULATION_START and line == _ENCAPSULATION_END:
            msg = f"VolInfo output line {n}: an empty encapsulated section (its decoding failed)"
            raise VolInfoError(msg)
        previous = line
        lzma += bool(_LZMA_SECTION.fullmatch(line))
        m = _FILE_NAME.fullmatch(line)
        if m:
            names.append(m.group(1).upper())
            continue
        t = _TOTAL.fullmatch(line)
        if t:
            totals.append(int(t.group(1)))
            this_fv += t.group(2) == "this"
            continue
        if not any(k.fullmatch(line) for k in _KNOWN):
            msg = f"VolInfo output line {n} not understood: {raw!r}"
            raise VolInfoError(msg)
    if this_fv != 1:
        msg = f"VolInfo output states {this_fv} volume totals, not one ('files in this FV')"
        raise VolInfoError(msg)
    if lzma != decoded:
        msg = f"VolInfo output has {lzma} LZMA sections but {decoded} decoded"
        raise VolInfoError(msg)
    if sum(totals) != len(names):
        msg = f"VolInfo output names {len(names)} files, but its totals add up to {sum(totals)}"
        raise VolInfoError(msg)
    return {g for g in names if g != FREE_SPACE}


def volume_offsets(data: bytes) -> list[int]:
    """The offsets of the image's top-level volumes, by their headers.

    A volume is skipped whole (its FvLength), so a nested one is VolInfo's
    to find.
    """
    offsets: list[int] = []
    pos = 0
    while (i := data.find(_SIGNATURE, pos)) >= 0:
        start = i - _SIGNATURE_OFFSET
        if start >= 0 and start + _HEADER_LENGTH + 2 <= len(data):
            (length,) = struct.unpack_from("<Q", data, start + _FV_LENGTH)
            (header,) = struct.unpack_from("<H", data, start + _HEADER_LENGTH)
            if header <= length and start + length <= len(data):
                offsets.append(start)
                pos = start + length
                continue
        pos = i + len(_SIGNATURE)
    return offsets


def image_guids(image: Path, tools_txt: Path, bin_dir: Path) -> set[str]:
    """The FFS file GUIDs of every volume in image, as VolInfo reads them.

    bin_dir is the build's BaseTools/BinWrappers/PosixLike (VolInfo and the
    decompressors); tools_txt the build's FV/GuidedSectionTools.txt.
    """
    data = image.read_bytes()
    offsets = volume_offsets(data)
    if not offsets:
        msg = f"{image}: no firmware volume found"
        raise VolInfoError(msg)
    volinfo = bin_dir / "VolInfo"
    # edk2's wrappers run $EDK_TOOLS_PATH/Source/C/bin/<tool> when it is
    # set: it must be the workspace's BaseTools, not the caller's.
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        EDK_TOOLS_PATH=str(bin_dir.parent.parent),
    )
    found: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="axlfw-volinfo-") as t:
        # VolInfo reads GuidedSectionTools.txt beside the file it reads.
        copy = Path(t) / image.name
        shutil.copyfile(image, copy)
        shutil.copyfile(tools_txt, Path(t) / "GuidedSectionTools.txt")
        for offset in offsets:
            try:
                r = subprocess.run(
                    [str(volinfo), "--offset", hex(offset), str(copy)],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=env,
                    cwd=t,
                    timeout=TIMEOUT,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as err:
                msg = f"{image}: VolInfo at {offset:#x}: {err}"
                raise VolInfoError(msg) from err
            if r.returncode != 0 or r.stderr.strip():
                msg = f"{image}: VolInfo at {offset:#x} exited {r.returncode}: {r.stderr.strip()[:500]}"
                raise VolInfoError(msg)
            try:
                found |= parse(r.stdout)
            except VolInfoError as err:
                msg = f"{image}: VolInfo at {offset:#x}: {err}"
                raise VolInfoError(msg) from err
    return found


def compare(image: Path, walker: set[str], volinfo: set[str]) -> list[str]:
    """The two readings' disagreement, said; none when they are equal."""
    problems: list[str] = []
    only_walker = sorted(walker - volinfo)
    only_volinfo = sorted(volinfo - walker)
    if only_walker:
        problems.append(f"{image}: the FV walker finds files VolInfo does not: {', '.join(only_walker)}")
    if only_volinfo:
        problems.append(f"{image}: VolInfo finds files the FV walker does not: {', '.join(only_volinfo)}")
    return problems
