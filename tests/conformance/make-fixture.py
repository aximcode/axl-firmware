#!/usr/bin/env python3
"""Generate the installer conformance fixture: tests/conformance/.

A miniature release in the real layout (docs/RELEASE-FORMAT.md), with
stand-in images of a few bytes, and a set of hostile releases, each with
the outcome an installer must give it, in expected.json. Shared by
axl-firmware's install.py (tests/test_conformance.py), axlvmpool's Rust
installer and axl-sdk, which vendor a copy (docs/DESIGN.md §4.29).

    tests/conformance/make-fixture.py [--check]

Writes cases/<name>/ (each case's tarball and SHA256SUMS) and expected.json
beside this script, replacing what is there; --check writes nothing, and
exits 1 naming each file that would change. The output is a function of
this script alone (and of lib/axlfw/install.py's descriptor shape): every
member is dated FIXTURE_EPOCH, owned 0:0, mode 0644 or 0755, in sorted
order, and each tarball is xz at preset 6 with a CRC64 check, so a run on
another host gives the same bytes unless its liblzma encodes differently
(then --check says which files differ, and the fixture is regenerated).

Standard library only; Python 3.9 or later.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "lib"))

from axlfw import install  # noqa: E402

FORMAT = 1
# A tag install.py's _TAG accepts, of no real edk2 release.
TAG = "edk2-stable200001"
REVISION = 1
RELEASE = f"{TAG}-r{REVISION}"
COMMIT = "0" * 39 + "1"
TOP = f"axl-firmware-{RELEASE}"
ASSET = f"{TOP}.tar.xz"
# 2000-01-01T00:00:00Z.
FIXTURE_EPOCH = 946684800
# The member size an installer must refuse (install.MAX_MEMBER), and the
# total of all members' sizes (install.MAX_TOTAL).
MAX_MEMBER = install.MAX_MEMBER
MAX_TOTAL = install.MAX_TOTAL
MAX_MEMBERS = install.MAX_MEMBERS
DIRS = (*install.IMAGE_DIRS, "guarded/x64", "guarded/aarch64")

Member = tarfile.TarInfo


class Zeros:
    """A member's data of n zero bytes, read as it is written (never held)."""

    def __init__(self, n: int) -> None:
        """n zero bytes."""
        self.size = n
        self._left = n

    def read(self, n: int = -1) -> bytes:
        """Up to n of the bytes left."""
        k = self._left if n < 0 else min(n, self._left)
        self._left -= k
        return bytes(k)


Data = Union[bytes, Zeros]


@dataclass
class Tree:
    """A release's members, in order: each TarInfo and its data."""

    entries: List[Tuple[Member, Data]] = field(default_factory=lambda: [])

    def add(self, info: Member, data: Data = b"", at: Optional[int] = None) -> None:
        """Insert info (and, for a file, its data) at index at, else at the end."""
        if info.isfile():
            info.size = data.size if isinstance(data, Zeros) else len(data)
        self.entries.insert(len(self.entries) if at is None else at, (info, data))

    def copy(self) -> Tree:
        """A tree of the same entries (the TarInfos shared: not changed after)."""
        return Tree(list(self.entries))

    def bytes_of(self, name: str) -> bytes:
        """The data of the first member called name."""
        data = next(d for i, d in self.entries if i.name == name)
        return data if isinstance(data, bytes) else bytes(data.size)


def _info(name: str, kind: bytes = tarfile.REGTYPE) -> Member:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mtime = FIXTURE_EPOCH
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    return info


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(rel: str, data: bytes) -> Dict[str, object]:
    return {"path": rel, "size": len(data), "sha256": _sha(data)}


def files(dirs: tuple[str, ...] = DIRS) -> Dict[str, bytes]:
    """The stand-in files of a release with image sets dirs, by path."""
    out: Dict[str, bytes] = {}
    for d in dirs:
        for key, rel in install.image_files(d).items():
            out[rel] = f"axl-firmware conformance fixture {RELEASE} {d} {key}\n".encode()
    for d in install.IMAGE_DIRS:
        desc = install.descriptor(d, RELEASE, TAG, COMMIT, "")
        out[f"descriptors/{install.descriptor_name(d, RELEASE)}"] = (json.dumps(desc, indent=2) + "\n").encode()
    out["LICENSES/edk2-License.txt"] = b"A stand-in for edk2's licence (BSD-2-Clause-Patent).\n"
    out["patches/0001-stand-in.patch"] = b"A stand-in patch.\n"
    return out


def manifest(content: Dict[str, bytes], dirs: tuple[str, ...] = DIRS) -> Dict[str, object]:
    """A format-2 manifest of content, every field RELEASE-FORMAT lists."""
    images: Dict[str, object] = {}
    for d in dirs:
        arch = d.rsplit("/", 1)[-1]
        flavour = "DEBUG" if d == arch else d.split("/", 1)[0].upper()
        described = d in install.IMAGE_DIRS
        images[d] = {
            "arch": arch,
            "flavour": flavour,
            "machine": "q35" if arch == "x64" else "virt",
            "platform": "OvmfPkg/OvmfPkgX64.dsc" if arch == "x64" else "ArmVirtPkg/ArmVirtQemu.dsc",
            "defines": [],
            "pcds": [],
            **{key: _entry(rel, content[rel]) for key, rel in install.image_files(d).items()},
            "descriptor": f"descriptors/{install.descriptor_name(d, RELEASE)}" if described else None,
            "secure_boot": False,
            "debug": {"target": "RELEASE" if flavour == "RELEASE" else "DEBUG"},
            "verified": {"by": "the conformance fixture", "drivers": [], "absent": []},
            "measured": {},
            "licenses": [{"path": "LICENSES/edk2-License.txt", "chosen": True}],
        }
    return {
        "format": install.FORMAT,
        "release": RELEASE,
        "edk2": {
            "tag": TAG,
            "commit": COMMIT,
            "url": "https://github.com/tianocore/edk2.git",
            "fixes": [],
            "patches": [{"name": "0001-stand-in.patch", "sha256": _sha(content["patches/0001-stand-in.patch"])}],
        },
        "flags_from": "the conformance fixture",
        "built": "2000-01-01T00:00:00Z",
        "built_by": {"how": "local", "axl_firmware_commit": "none", "uncommitted_changes": -1},
        "toolchain": "GCC",
        "compilers": {"x64": "none", "aarch64": "none"},
        "default_flavour": "DEBUG",
        "hardware_images": {"x64": "release/x64", "aarch64": "release/aarch64"},
        "images": images,
        "descriptors": [f"descriptors/{install.descriptor_name(d, RELEASE)}" for d in install.IMAGE_DIRS],
        "symbols": {"x64": f"{TOP}-symbols-x64.tar.xz", "aarch64": f"{TOP}-symbols-aarch64.tar.xz"},
        "licenses": ["LICENSES/edk2-License.txt"],
    }


def tree(
    content: Optional[Dict[str, bytes]] = None,
    edit: Optional[Callable[[Dict[str, object]], None]] = None,
    dirs: tuple[str, ...] = DIRS,
    top: str = TOP,
    drop: tuple[str, ...] = (),
) -> Tree:
    """A release's members as RELEASE-FORMAT orders them: top/, then every
    directory and file under it by path components; edit changes the
    manifest first; drop leaves files out, the manifest listing them."""
    content = dict(files(dirs) if content is None else content)
    m = manifest(content, dirs)
    if edit:
        edit(m)
    for rel in drop:
        del content[rel]
    content["manifest.json"] = (json.dumps(m, indent=2) + "\n").encode()
    paths = set(content)
    for rel in list(content):
        parts = rel.split("/")
        paths.update("/".join(parts[:n]) for n in range(1, len(parts)))
    t = Tree()
    t.add(_info(top, tarfile.DIRTYPE))
    for rel in sorted(paths, key=lambda p: p.split("/")):
        if rel in content:
            t.add(_info(f"{top}/{rel}"), content[rel])
        else:
            t.add(_info(f"{top}/{rel}", tarfile.DIRTYPE))
    return t


class _Sink(io.RawIOBase):
    """A writable stream into an xz compressor (or none: the tar itself)."""

    def __init__(self, preset: Optional[int]) -> None:
        super().__init__()
        self._c = None if preset is None else lzma.LZMACompressor(lzma.FORMAT_XZ, lzma.CHECK_CRC64, preset)
        self.out = io.BytesIO()

    def writable(self) -> bool:
        return True

    def write(self, b: bytes) -> int:  # type: ignore[override]
        self.out.write(b if self._c is None else self._c.compress(bytes(b)))
        return len(b)

    def done(self) -> bytes:
        if self._c is not None:
            self.out.write(self._c.flush())
        return self.out.getvalue()


def tarball(t: Tree, preset: Optional[int] = 6) -> bytes:
    """t as a tar (PAX format, as Python writes by default), xz-compressed at
    preset with a CRC64 check, as every fixture tarball is; preset None, the
    tar uncompressed. Streamed: a Zeros member is never held whole."""
    sink = _Sink(preset)
    with tarfile.open(fileobj=sink, mode="w|", format=tarfile.PAX_FORMAT) as tar:  # type: ignore[arg-type]
        for info, data in t.entries:
            tar.addfile(info, (data if isinstance(data, Zeros) else io.BytesIO(data)) if info.isfile() else None)
    return sink.done()


def sums(name: str, data: bytes) -> bytes:
    """A SHA256SUMS listing name."""
    return f"{_sha(data)}  {name}\n".encode()


# ---------------------------------------------------------------- the cases


@dataclass
class Case:
    """One release given to an installer, and what it must do with it.

    outcome: "installed", "refused" or "waits" (for another install's lock).
    stage: where the refusal comes, in install.py's order (item 105):
    "digest", "members", "manifest", "descriptors". reason: a stable token
    for the refusal, for an installer that words its own messages. message:
    install.py's error, exactly, with {tarball} for the tarball's path as
    given, and {unpacked} for the private unpack directory's release
    (<prefix>/.install.<random>/<top>, the prefix resolved).
    """

    name: str
    what: str
    outcome: str
    tarball: bytes
    installed: Optional[Tree] = None
    asset: str = ASSET
    sha256sums: Optional[bytes] = None
    pin: Optional[str] = None
    stage: Optional[str] = None
    reason: Optional[str] = None
    message: Optional[str] = None

    def files(self) -> Dict[str, bytes]:
        """The case's directory: its tarball and, unless pinned, SHA256SUMS."""
        out = {self.asset: self.tarball}
        if self.sha256sums is not None:
            out["SHA256SUMS"] = self.sha256sums
        return out


def _release(t: Tree, asset: str = ASSET, preset: int = 6) -> tuple[bytes, bytes]:
    data = tarball(t, preset)
    return data, sums(asset, data)


def _refused(name: str, what: str, t: Tree, stage: str, reason: str, message: str, preset: int = 6) -> Case:
    data, s = _release(t, preset=preset)
    return Case(name, what, "refused", data, sha256sums=s, stage=stage, reason=reason, message=message)


def _with(t: Tree, info: Member, data: Data = b"") -> Tree:
    """t with one more member, at the end."""
    out = t.copy()
    out.add(info, data)
    return out


def _member(name: str, kind: bytes = tarfile.REGTYPE, link: str = "") -> Member:
    info = _info(name, kind)
    info.linkname = link
    return info


def _odd(info: Member) -> Member:
    """A copy of info with a hostile mode and owner."""
    out = tarfile.TarInfo(info.name)
    for attr in ("type", "mtime", "size", "linkname"):
        setattr(out, attr, getattr(info, attr))
    out.mode = 0o7777 if info.isdir() else 0o6777
    out.uid = out.gid = 1000
    out.uname = out.gname = "someone"
    return out


def cases() -> List[Case]:
    """Every case, the good ones first."""
    good = tree()
    good_data, good_sums = _release(good)
    out: List[Case] = [
        Case("good", "the miniature release, r3's shape: six image sets, four descriptors", "installed",
             good_data, good, sha256sums=good_sums),
        Case("good-pinned", "the same, its sha256 pinned and no SHA256SUMS", "installed", good_data, good,
             pin=_sha(good_data)),
    ]
    four = tree(dirs=install.IMAGE_DIRS)
    r2_data, r2_sums = _release(four)
    out.append(Case("good-four-sets", "r2's shape: the four image sets only", "installed", r2_data, four,
                    sha256sums=r2_sums))
    # Hostile modes and owners: setuid, world-writable, another owner. An
    # installer writes files 0644 and directories 0755, as the user.
    odd = Tree([(_odd(i), d) for i, d in good.entries])
    odd_data, odd_sums = _release(odd)
    out.append(Case("hostile-modes", "every member setuid and world-writable, owned by 1000:1000 (named)", "installed",
                    odd_data, good, sha256sums=odd_sums))
    out.append(Case("locked", "the good release, while another install holds <prefix>/.lock", "waits",
                    good_data, good, sha256sums=good_sums))

    # The digest of the tarball: the trust anchor.
    out.append(Case("sha256-mismatch", "SHA256SUMS lists another digest for the tarball", "refused", good_data,
                    sha256sums=sums(ASSET, b"another tarball"), stage="digest", reason="digest",
                    message=f"{{tarball}}: sha256 {_sha(good_data)}, expected {_sha(b'another tarball')}"))
    out.append(Case("pin-mismatch", "a pinned sha256 that is not the tarball's", "refused", good_data,
                    pin="0" * 64, stage="digest", reason="digest",
                    message=f"{{tarball}}: sha256 {_sha(good_data)}, expected {'0' * 64}"))
    out.append(Case("sums-without-asset", "SHA256SUMS that does not list the tarball", "refused", good_data,
                    sha256sums=sums("other.tar.xz", good_data), stage="digest", reason="digest",
                    message=f"{{tarball_dir}}/SHA256SUMS does not list {ASSET}"))
    out.append(Case("no-sums", "no SHA256SUMS and no pin", "refused", good_data, stage="digest", reason="digest",
                    message="{tarball}: no SHA256SUMS beside it; give its sha256 with --sha256"))
    truncated = good_data[: len(good_data) // 2]
    out.append(Case("truncated-xz", "the tarball cut in half, SHA256SUMS of the cut file", "refused", truncated,
                    sha256sums=sums(ASSET, truncated), stage="members", reason="corrupt",
                    message="{tarball}: not a complete xz-compressed tar: Compressed file ended before the "
                            "end-of-stream marker was reached"))
    not_xz = tarball(good, None)
    out.append(Case("not-xz", "an uncompressed tar named .tar.xz", "refused", not_xz,
                    sha256sums=sums(ASSET, not_xz), stage="members", reason="corrupt",
                    message="{tarball}: not a complete xz-compressed tar: not an lzma file"))
    bad_name = "axl-firmware-latest.tar.xz"
    out.append(Case("bad-asset-name", "a tarball not named axl-firmware-<release>.tar.xz", "refused", good_data,
                    asset=bad_name, sha256sums=sums(bad_name, good_data), stage="digest", reason="name",
                    message="{tarball}: not named axl-firmware-<release>.tar.xz, with a release edk2-stableYYYYMM-rN"))

    # The members.
    def member_case(name: str, what: str, info: Member, data: bytes = b"") -> Case:
        return _refused(name, what, _with(good, info, data), "members", "member",
                        f"{{tarball}}: unexpected member {info.name!r}")

    out.append(member_case("member-dotdot", "a member ../escape", _member("../escape"), b"x"))
    out.append(member_case("member-dotdot-inside", "a member under the top directory climbing out of it",
                           _member(f"{TOP}/../../escape"), b"x"))
    out.append(member_case("member-absolute", "a member /tmp/axl-firmware-escape", _member("/tmp/axl-firmware-escape"),
                           b"x"))
    out.append(member_case("member-outside-top", "a member beside the top directory", _member("elsewhere/file"), b"x"))
    out.append(member_case("member-other-top", "every member under another release's top directory... one of them",
                           _member(f"axl-firmware-{TAG}-r2/x64/code.fd"), b"x"))
    out.append(member_case("symlink-escaping", "a symlink in the release to /etc/passwd",
                           _member(f"{TOP}/x64/link", tarfile.SYMTYPE, "/etc/passwd")))
    out.append(member_case("symlink-inside", "a symlink in the release to a file of its own",
                           _member(f"{TOP}/x64/link", tarfile.SYMTYPE, "code.fd")))
    out.append(member_case("hardlink-escaping", "a hard link in the release to ../../etc/passwd",
                           _member(f"{TOP}/x64/link", tarfile.LNKTYPE, "../../etc/passwd")))
    out.append(member_case("hardlink-inside", "a hard link in the release to a file of its own",
                           _member(f"{TOP}/x64/link", tarfile.LNKTYPE, f"{TOP}/x64/code.fd")))
    out.append(member_case("char-device", "a character device node", _member(f"{TOP}/x64/null", tarfile.CHRTYPE)))
    out.append(member_case("block-device", "a block device node", _member(f"{TOP}/x64/disk", tarfile.BLKTYPE)))
    out.append(member_case("fifo", "a FIFO", _member(f"{TOP}/x64/fifo", tarfile.FIFOTYPE)))
    pax = _member(f"{TOP}/x64/innocent")
    pax.pax_headers = {"path": f"{TOP}/../../escape"}
    out.append(_refused("pax-path-dotdot", "a PAX header whose path climbs out (the ustar name is harmless)",
                        _with(good, pax, b"x"), "members", "member", f"{{tarball}}: unexpected member '{TOP}/../../escape'"))
    # A second x64/code.fd, other bytes, after the first (the one the
    # manifest records): which one an installer keeps decides what is
    # installed, so the release is refused.
    dup = good.copy()
    dup.add(_member(f"{TOP}/x64/code.fd"), b"evil", at=len(dup.entries) - 1)
    out.append(_refused("duplicate-member", "x64/code.fd twice, the second other bytes (the manifest's first)",
                        dup, "members", "duplicate", f"{{tarball}}: member '{TOP}/x64/code.fd' appears twice"))
    # Zeros, xz at preset 0: a few hundred KiB, each decompressing to more
    # than an installer may write.
    big = _member(f"{TOP}/x64/big.bin")
    out.append(_refused("oversize-member", f"a member of {MAX_MEMBER + 1} bytes (zeros), over the cap",
                        _with(good, big, Zeros(MAX_MEMBER + 1)), "members", "oversize",
                        f"{{tarball}}: member '{TOP}/x64/big.bin' is {MAX_MEMBER + 1} bytes, over {MAX_MEMBER}",
                        preset=0))
    many = good
    n = MAX_TOTAL // MAX_MEMBER + 1
    for i in range(n):
        many = _with(many, _member(f"{TOP}/x64/part{i}.bin"), Zeros(MAX_MEMBER))
    crowd = good
    for i in range(MAX_MEMBERS + 1 - len(good.entries)):
        crowd = _with(crowd, _member(f"{TOP}/x64/e{i}"))
    out.append(_refused("too-many-members", f"{MAX_MEMBERS + 1} members, empty files, over the count cap", crowd,
                        "members", "count", f"{{tarball}}: more than {MAX_MEMBERS} members"))
    out.append(_refused("oversize-total", f"{n} members of {MAX_MEMBER} bytes each (zeros), over the total cap",
                        many, "members", "oversize",
                        f"{{tarball}}: its members total more than {MAX_TOTAL} bytes", preset=0))

    # The manifest.
    def manifest_case(name: str, what: str, edit: Callable[[Dict[str, object]], None], message: str,
                      reason: str = "manifest") -> Case:
        return _refused(name, what, tree(edit=edit), "manifest", reason, message)

    def fmt(v: int) -> Callable[[Dict[str, object]], None]:
        def edit(m: Dict[str, object]) -> None:
            m["format"] = v
        return edit

    out.append(manifest_case("format-1", "manifest format 1", fmt(1),
                             "{unpacked}: manifest format 1; this install reads format 2", "format"))
    out.append(manifest_case("format-3", "manifest format 3", fmt(3),
                             "{unpacked}: manifest format 3; this install reads format 2", "format"))

    def other_release(m: Dict[str, object]) -> None:
        m["release"] = f"{TAG}-r2"

    out.append(manifest_case("release-mismatch", "a manifest naming another release than the tarball's", other_release,
                             f"{{unpacked}}: its manifest names release '{TAG}-r2', not '{RELEASE}'", "release"))

    def images(m: Dict[str, object]) -> Dict[str, Dict[str, Dict[str, object]]]:
        return m["images"]  # type: ignore[return-value]

    def missing_set(m: Dict[str, object]) -> None:
        del images(m)["release/aarch64"]

    have = sorted(d for d in DIRS if d != "release/aarch64")
    out.append(manifest_case("missing-set", "no release/aarch64 in the manifest", missing_set,
                             f"{{unpacked}}: its manifest's images are {have}, without ['release/aarch64']", "set"))

    def bad_set(m: Dict[str, object]) -> None:
        images(m)["../x64"] = images(m)["x64"]

    out.append(manifest_case("set-escaping", "an image set ../x64 in the manifest", bad_set,
                             "{unpacked}: its manifest lists an image set '../x64', not <name>/<x64|aarch64>", "set"))

    def newline_set(m: Dict[str, object]) -> None:
        images(m)["guarded/x64\n"] = images(m)["guarded/x64"]

    out.append(manifest_case("set-newline", "an image set 'guarded/x64' with a trailing newline", newline_set,
                             "{unpacked}: its manifest lists an image set 'guarded/x64\\n', not <name>/<x64|aarch64>",
                             "set"))

    def reserved_set(m: Dict[str, object]) -> None:
        images(m)["symbols/x64"] = images(m)["guarded/x64"]

    out.append(manifest_case("set-reserved", "an image set symbols/x64, a name the install layout uses", reserved_set,
                             "{unpacked}: its manifest lists an image set 'symbols/x64', a name the release layout"
                             " reserves", "set"))

    def code_moved(m: Dict[str, object]) -> None:
        images(m)["x64"]["code"] = images(m)["release/x64"]["code"]

    out.append(manifest_case("image-moved", "x64's code naming release/x64's file, its size and sha256 true of it",
                             code_moved,
                             "{unpacked}: its manifest names 'release/x64/code.fd' for x64's code, not 'x64/code.fd'",
                             "image"))

    def digest(m: Dict[str, object]) -> None:
        images(m)["aarch64"]["vars"]["sha256"] = "0" * 64

    out.append(manifest_case("image-digest", "aarch64's vars with another sha256 in the manifest", digest,
                             "{unpacked}/aarch64/vars.fd: not aarch64's vars as its manifest records it", "image"))

    def guarded_digest(m: Dict[str, object]) -> None:
        images(m)["guarded/x64"]["shell"]["size"] = 1

    out.append(manifest_case("guarded-image-size", "guarded/x64's Shell with another size in the manifest",
                             guarded_digest,
                             "{unpacked}/guarded/x64/Shell_X64.efi: not guarded/x64's shell as its manifest records it",
                             "image"))
    out.append(_refused("image-missing", "release/x64/vars.fd absent, the manifest listing it",
                        tree(drop=("release/x64/vars.fd",)), "manifest", "image",
                        "{unpacked}/release/x64/vars.fd: not release/x64's vars as its manifest records it"))

    # The descriptors.
    def descriptor_case(name: str, what: str, change: Callable[[Dict[str, object]], None], message: str) -> Case:
        content = files()
        rel = f"descriptors/{install.descriptor_name('x64', RELEASE)}"
        desc = json.loads(content[rel])
        change(desc)
        content[rel] = (json.dumps(desc, indent=2) + "\n").encode()
        return _refused(name, what, tree(content=content), "descriptors", "descriptor", message)

    def outside(d: Dict[str, object]) -> None:
        d["mapping"]["executable"]["filename"] = "../../etc/passwd"  # type: ignore[index]

    x64_desc = f"{{unpacked}}/descriptors/{install.descriptor_name('x64', RELEASE)}"
    out.append(descriptor_case("descriptor-outside", "x64's descriptor naming ../../etc/passwd", outside,
                               f"{x64_desc}: its executable is '../../etc/passwd', not 'x64/code.fd'"))

    def absolute(d: Dict[str, object]) -> None:
        d["mapping"]["nvram-template"]["filename"] = "/etc/passwd"  # type: ignore[index]

    out.append(descriptor_case("descriptor-absolute", "x64's descriptor naming /etc/passwd as its vars", absolute,
                               f"{x64_desc}: its nvram-template is '/etc/passwd', not 'x64/vars.fd'"))

    def other_arch(d: Dict[str, object]) -> None:
        d["targets"][0]["architecture"] = "aarch64"  # type: ignore[index]

    out.append(descriptor_case("descriptor-retargeted", "x64's descriptor claiming aarch64", other_arch,
                               f"{x64_desc}: not the descriptor of x64"))

    def no_descriptors(m: Dict[str, object]) -> None:
        m["descriptors"] = []

    want = sorted(f"descriptors/{install.descriptor_name(d, RELEASE)}" for d in install.IMAGE_DIRS)
    out.append(manifest_case("descriptors-unlisted", "a manifest listing no descriptor", no_descriptors,
                             f"{{unpacked}}: its manifest's descriptors are [], not {want}", "descriptor"))
    return out


def outputs() -> Dict[str, bytes]:
    """Every file the fixture holds, by path relative to this directory."""
    out: Dict[str, bytes] = {}
    listed: List[Dict[str, object]] = []
    for c in cases():
        for name, data in c.files().items():
            out[f"cases/{c.name}/{name}"] = data
        entry: Dict[str, object] = {
            "name": c.name,
            "what": c.what,
            "tarball": f"cases/{c.name}/{c.asset}",
            "sha256sums": f"cases/{c.name}/SHA256SUMS" if c.sha256sums is not None else None,
            "pin": c.pin,
            "outcome": c.outcome,
        }
        if c.outcome == "refused":
            entry.update(stage=c.stage, reason=c.reason, message=c.message)
        if c.installed is not None:
            t = c.installed
            entry["files"] = {f"{RELEASE}/{m.name[len(TOP) + 1:]}": _sha(t.bytes_of(m.name)) for m, _ in t.entries
                              if m.isfile()}
        listed.append(entry)
    descriptors = {
        install.descriptor_name(d, RELEASE): install.descriptor(d, RELEASE, TAG, COMMIT, f"{{prefix}}/{RELEASE}/")
        for d in install.IMAGE_DIRS
    }
    expected = {
        "format": FORMAT,
        "generator": "tests/conformance/make-fixture.py",
        "release": RELEASE,
        "max_member": MAX_MEMBER,
        "max_total": MAX_TOTAL,
        "max_members": MAX_MEMBERS,
        "installed": {
            "files": "each installed or waiting case's files: every file under <prefix>, by path, its sha256",
            "descriptors": descriptors,
            "descriptors_removed": "every other 10-axl-firmware-*.json and 20-axl-firmware-*.json in"
                                   " <prefix>/descriptors; other files there, and other releases' directories, kept",
        },
        "refused": "the prefix as it was: every path under it and each file's bytes (directory mtimes aside)",
        "waits": "no change to the prefix while the lock is held; once it is released, installed",
        "cases": listed,
    }
    out["expected.json"] = (json.dumps(expected, indent=2, sort_keys=False) + "\n").encode()
    return out


def main(argv: List[str]) -> int:
    """Write the fixture, or with --check say what would change."""
    p = argparse.ArgumentParser(prog="make-fixture.py")
    p.add_argument("--check", action="store_true", help="write nothing; exit 1 if any file would change")
    args = p.parse_args(argv)
    want = outputs()
    have = {str(f.relative_to(HERE)): f.read_bytes() for f in (HERE / "cases").rglob("*") if f.is_file()}
    if (HERE / "expected.json").is_file():
        have["expected.json"] = (HERE / "expected.json").read_bytes()
    differ = sorted(n for n in set(want) | set(have) if want.get(n) != have.get(n))
    if args.check:
        for n in differ:
            print(f"make-fixture.py: {n} {'differs' if n in want and n in have else 'missing' if n in want else 'extra'}",
                  file=sys.stderr)
        return 1 if differ else 0
    for n in differ:
        f = HERE / n
        if n not in want:
            f.unlink()
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(want[n])
    for d in sorted((HERE / "cases").iterdir()) if (HERE / "cases").is_dir() else []:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    print(f"make-fixture.py: {len(want)} files, {len(differ)} written or removed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
