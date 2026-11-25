"""Assemble a release from a finished build, and re-check an unpacked one.

The release (README.md, "Release contents"):

    axl-firmware-<release>/
      x64/{code.fd,vars.fd,Shell_X64.efi}              DEBUG, the default
      aarch64/{code.fd,vars.fd,Shell_AARCH64.efi}      DEBUG
      release/x64/...  release/aarch64/...             RELEASE
      descriptors/10-axl-firmware-<release>-<arch>.json          DEBUG
      descriptors/20-axl-firmware-<release>-release-<arch>.json  RELEASE
      manifest.json  LICENSES/  patches/

Descriptors in the tarball name their images relative to that directory;
the install makes them absolute.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import os
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from axlfw import boot, checks

FORMAT = 2
AARCH64_FLASH = 64 * 1024 * 1024
FLAGS_FROM = (
    "QEMU v10.0.0 roms/edk2-build.config, plus edk2-stable202408's front page "
    "(PcdBootManagerMenuFile, PcdSetupConOut*, PcdSetupVideo*)"
)


@dataclass(frozen=True)
class Image:
    """One arch and flavour: where its build is, and where it goes."""

    arch: str
    flavour: str

    @property
    def dir(self) -> str:
        """Its directory in the release."""
        return self.arch if self.flavour == "DEBUG" else f"release/{self.arch}"

    @property
    def shell(self) -> str:
        """The Shell's file name."""
        return f"Shell_{self.arch.upper()}.efi"

    @property
    def machine(self) -> str:
        """QEMU's machine type."""
        return "q35" if self.arch == "x64" else "virt"

    @property
    def platform(self) -> str:
        """The DSC it is built from."""
        return "OvmfPkg/OvmfPkgX64.dsc" if self.arch == "x64" else "ArmVirtPkg/ArmVirtQemu.dsc"

    def build(self, ws: Path, toolchain: str) -> Path:
        """Its edk2 build directory."""
        out = "OvmfX64" if self.arch == "x64" else "ArmVirtQemu-AArch64"
        return ws / "Build" / out / f"{self.flavour}_{toolchain}"

    def descriptor(self, release: str) -> str:
        """Its QEMU firmware descriptor's file name: DEBUG first by name."""
        if self.flavour == "DEBUG":
            return f"10-axl-firmware-{release}-{self.arch}.json"
        return f"20-axl-firmware-{release}-release-{self.arch}.json"


IMAGES = tuple(Image(a, f) for f in checks.FLAVOURS for a in checks.ARCHES)


def sha256(path: Path) -> str:
    """The file's sha256, hex."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def file_entry(root: Path, rel: str) -> dict[str, object]:
    """A file's manifest entry: path, size, sha256."""
    p = root / rel
    return {"path": rel, "size": p.stat().st_size, "sha256": sha256(p)}


def features(img: Image) -> list[str]:
    """The descriptor's features: only what the build has.

    X64 as QEMU labels its own build with these flags (S3, SEV, SEV-ES);
    DEBUG adds its debug output: X64's on the 0x402 debug port
    (verbose-dynamic), AARCH64's on the serial port by static configuration
    (verbose-static; errors only, but static serial output all the same).
    RELEASE has none.
    """
    base = ["acpi-s3", "amd-sev", "amd-sev-es"] if img.arch == "x64" else []
    if img.flavour == "DEBUG":
        base.append("verbose-dynamic" if img.arch == "x64" else "verbose-static")
    return base


def descriptor(img: Image, release: str, tag: str, commit: str, root: str) -> dict[str, object]:
    """The QEMU firmware descriptor (docs/interop/firmware.json) of img.

    root prefixes the images' paths: "" in the tarball, the install
    directory (with a trailing slash) once installed.
    """
    what = "X64 (q35)" if img.arch == "x64" else "AARCH64 (virt)"
    return {
        "description": f"axl-firmware {release} for {what}, {img.flavour}, edk2 {tag}",
        "interface-types": ["uefi"],
        "mapping": {
            "device": "flash",
            "mode": "split",
            "executable": {"filename": f"{root}{img.dir}/code.fd", "format": "raw"},
            "nvram-template": {"filename": f"{root}{img.dir}/vars.fd", "format": "raw"},
        },
        "targets": [
            {
                "architecture": "x86_64" if img.arch == "x64" else "aarch64",
                "machines": ["pc-q35-*"] if img.arch == "x64" else ["virt-*"],
            }
        ],
        "features": features(img),
        "tags": [tag, commit, release, img.flavour],
    }


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def stage_images(ws: Path, toolchain: str, stage: Path) -> list[str]:
    """Copy each image set into stage; pad AARCH64's to its 64 MiB banks."""
    problems: list[str] = []
    for img in IMAGES:
        b = img.build(ws, toolchain)
        d = stage / img.dir
        d.mkdir(parents=True)
        arch_dir = "X64" if img.arch == "x64" else "AARCH64"
        code, vars_ = ("OVMF_CODE.fd", "OVMF_VARS.fd") if img.arch == "x64" else ("QEMU_EFI.fd", "QEMU_VARS.fd")
        shutil.copyfile(b / "FV" / code, d / "code.fd")
        shutil.copyfile(b / "FV" / vars_, d / "vars.fd")
        shutil.copyfile(b / arch_dir / "Shell.efi", d / img.shell)
        if img.arch == "aarch64":
            # QEMU's pad1/pad2: a virt machine's pflash banks are 64 MiB; an
            # image larger than that is refused, not cut.
            for f in (d / "code.fd", d / "vars.fd"):
                size = f.stat().st_size
                if size > AARCH64_FLASH:
                    problems.append(f"{f}: {size} bytes, larger than its 64 MiB flash bank")
                    continue
                with f.open("r+b") as fh:
                    fh.truncate(AARCH64_FLASH)
    return problems


def licenses(src: Path, ws: Path, stage: Path) -> list[str]:
    """edk2's licence, and each bundled third-party part's that the build used.

    A submodule counts as built in when a module's Makefile names a path
    in it. Returns the files copied, relative to stage.
    """
    out = stage / "LICENSES"
    out.mkdir()
    shutil.copyfile(src / "License.txt", out / "edk2-License.txt")
    copied = ["LICENSES/edk2-License.txt"]
    makefiles = [p.read_text(errors="replace") for p in (ws / "Build").rglob("Makefile")]
    gitmodules = (src / ".gitmodules").read_text()
    paths = [line.split("=", 1)[1].strip() for line in gitmodules.splitlines() if line.strip().startswith("path")]
    for sub in sorted(paths):
        if not any(f"{sub}/" in m for m in makefiles):
            continue
        names = sorted(
            p
            for p in (src / sub).iterdir()
            if p.is_file() and p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "LICENCE"))
        )
        dest = out / sub.replace("/", "_")
        dest.mkdir()
        for p in names:
            shutil.copyfile(p, dest / p.name)
            copied.append(f"LICENSES/{dest.name}/{p.name}")
    return copied


# The symbols a debugger needs first: the DXE core, BDS and the Shell.
SYMBOLS_REQUIRED = ("DxeCore.debug", "BdsDxe.debug", "Shell.debug")


def check_symbols(dest: Path) -> list[str]:
    """An arch's symbols: the core modules' ELF .debug files and FV maps."""
    problems: list[str] = []
    for name in SYMBOLS_REQUIRED:
        f = dest / name
        if not f.is_file():
            problems.append(f"{dest}: no {name}")
        elif f.read_bytes()[:4] != b"\x7fELF":
            problems.append(f"{f}: not an ELF file")
    if not list((dest / "FV").glob("*.Fv.map")):
        problems.append(f"{dest}/FV: no firmware-volume maps")
    return problems


def static_checks(stage: Path) -> tuple[dict[str, dict[str, object]], list[str]]:
    """The image and Shell checks of every image set in stage."""
    verified: dict[str, dict[str, object]] = {}
    problems: list[str] = []
    for img in IMAGES:
        d = stage / img.dir
        problems += checks.check_image(img.arch, d / "code.fd")
        problems += checks.check_shell(img.arch, d / img.shell)
        verified[img.dir] = {
            "by": "uefi_firmware 1.17 (lib/axlfw/fv.py)",
            "drivers": [{"name": x.name, "guid": x.guid} for x in checks.drivers(img.arch)],
            "absent": [{"name": x.name, "guid": x.guid} for x in checks.ABSENT],
        }
    return verified, problems


def boot_checks(
    root: Path, tag: str, qemu_dir: Path | None, logs: Path
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Every image set's boot checks, AXLFW_BOOT_JOBS (default 2) at a time."""
    jobs = int(os.environ.get("AXLFW_BOOT_JOBS", "2"))
    timeout = float(os.environ.get("AXLFW_BOOT_TIMEOUT", "180"))
    facts: dict[str, dict[str, object]] = {}
    problems: list[str] = []

    def one(img: Image) -> boot.Result:
        d = root / img.dir
        s = boot.ImageSet(img.arch, img.flavour, d / "code.fd", d / "vars.fd", d / img.shell, tag)
        return boot.run(s, qemu_dir, logs, timeout)

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(one, IMAGES))
    for img, r in zip(IMAGES, results):
        facts[img.dir] = r.facts
        problems += r.problems
    return facts, problems


def _tar(out: Path, root: Path, prefix: str, epoch: int) -> None:
    """A reproducible tar.xz of root's files under prefix/."""
    with tarfile.open(out, "w:xz", preset=9) as tar:
        names = sorted(p for p in root.rglob("*"))
        for p in [root, *names]:
            rel = p.relative_to(root).as_posix()
            info = tar.gettarinfo(str(p), arcname=prefix if rel == "." else f"{prefix}/{rel}")
            info.mtime = epoch
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if p.is_dir() else 0o644
            if p.is_file():
                with p.open("rb") as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)


def assemble(a: AssembleArgs) -> int:
    """Stage, check, measure, describe and pack a finished build."""
    rel = a.release
    if a.stage.exists():
        shutil.rmtree(a.stage)
    stage = a.stage / f"axl-firmware-{rel}"
    stage.mkdir(parents=True)
    problems = stage_images(a.ws, a.toolchain, stage)
    verified, more = static_checks(stage)
    problems += more
    debug: dict[str, dict[str, object]] = {}
    for img in IMAGES:
        facts, more = checks.build_facts(img.build(a.ws, a.toolchain), img.arch, img.flavour)
        debug[img.dir] = facts
        problems += more
    if problems:
        print("\n".join(f"FAIL: {p}" for p in problems), file=sys.stderr)
        return 1
    print("+ static checks passed: drivers, absent files, Shells, AutoGen", flush=True)
    measured, problems = boot_checks(stage, a.tag, a.qemu_dir, a.stage / "boot-logs")
    if problems:
        print("\n".join(f"FAIL: {p}" for p in problems), file=sys.stderr)
        print(f"(console transcripts in {a.stage / 'boot-logs'})", file=sys.stderr)
        return 1
    print("+ boot checks passed", flush=True)

    (stage / "patches").mkdir()
    for p in a.patches:
        shutil.copyfile(p, stage / "patches" / p.name)
    lic = licenses(a.src, a.ws, stage)
    (stage / "descriptors").mkdir()
    for img in IMAGES:
        _json(stage / "descriptors" / img.descriptor(rel), descriptor(img, rel, a.tag, a.commit, ""))
    images: dict[str, object] = {}
    for img in IMAGES:
        d = img.dir
        dbg = debug[d]
        if img.flavour == "DEBUG":
            dbg["output"] = (
                "debugcon I/O port 0x402" if img.arch == "x64" else "the second PL011 UART, when QEMU has one"
            )
            dbg["descriptor_feature"] = "verbose-dynamic" if img.arch == "x64" else "verbose-static"
        m = measured[d]
        images[d] = {
            "arch": img.arch,
            "flavour": img.flavour,
            "machine": img.machine,
            "platform": img.platform,
            "defines": a.defines[img.arch].split(),
            "pcds": [
                f"PcdFirmwareVersionString=L{a.tag}\\0",
                f"PcdFirmwareReleaseDateString=L{a.release_date}\\0",
                *a.pcds[img.arch].split(),
            ],
            "code": file_entry(stage, f"{d}/code.fd"),
            "vars": file_entry(stage, f"{d}/vars.fd"),
            "shell": file_entry(stage, f"{d}/{img.shell}"),
            "descriptor": f"descriptors/{img.descriptor(rel)}",
            "debug": dbg,
            "secure_boot": False,
            "verified": verified[d],
            "measured": m,
        }
    epoch = int(datetime.datetime.strptime(a.release_date, "%m/%d/%Y").replace(tzinfo=datetime.timezone.utc).timestamp())
    symbols: dict[str, str] = {}
    a.dist.mkdir(parents=True, exist_ok=True)
    for old in a.dist.glob("axl-firmware-*"):
        old.unlink()
    for arch in checks.ARCHES:
        img = Image(arch, "DEBUG")
        b = img.build(a.ws, a.toolchain)
        sroot = a.stage / f"symbols-{arch}"
        dest = sroot / arch
        (dest / "FV").mkdir(parents=True)
        for f in sorted((b / arch.upper()).glob("*.debug")):
            shutil.copyfile(f, dest / f.name)
        for f in sorted((b / "FV").glob("*.Fv.map")):
            shutil.copyfile(f, dest / "FV" / f.name)
        problems = check_symbols(dest)
        if problems:
            print("\n".join(f"FAIL: {p}" for p in problems), file=sys.stderr)
            return 1
        name = f"axl-firmware-{rel}-symbols-{arch}.tar.xz"
        _tar(a.dist / name, sroot, f"axl-firmware-{rel}-symbols-{arch}", epoch)
        symbols[arch] = name
    manifest: dict[str, object] = {
        "format": FORMAT,
        "release": rel,
        "edk2": {
            "tag": a.tag,
            "commit": a.commit,
            "url": a.url,
            "fixes": [a.fix],
            "patches": [{"name": p.name, "sha256": sha256(p)} for p in a.patches],
        },
        "flags_from": FLAGS_FROM,
        "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "built_by": {"how": a.built_by, "axl_firmware_commit": a.repo_commit, "uncommitted_changes": a.repo_dirty},
        "toolchain": a.toolchain,
        "compilers": a.compilers,
        "default_flavour": "DEBUG",
        "images": images,
        "descriptors": [f"descriptors/{img.descriptor(rel)}" for img in IMAGES],
        "symbols": symbols,
        "licenses": lic,
    }
    _json(stage / "manifest.json", manifest)
    main = f"axl-firmware-{rel}.tar.xz"
    _tar(a.dist / main, stage, f"axl-firmware-{rel}", epoch)
    sums = "".join(f"{sha256(a.dist / n)}  {n}\n" for n in sorted([main, *symbols.values()]))
    (a.dist / "SHA256SUMS").write_text(sums)
    print(sums, end="")
    print(f"+ packed {a.dist}")
    return 0


@dataclass
class AssembleArgs:
    """assemble's inputs, from build.sh."""

    ws: Path
    src: Path
    dist: Path
    stage: Path
    release: str
    tag: str
    commit: str
    url: str
    fix: str
    toolchain: str
    release_date: str
    compilers: dict[str, str]
    defines: dict[str, str]
    pcds: dict[str, str]
    patches: list[Path]
    repo_commit: str
    repo_dirty: int
    built_by: str
    qemu_dir: Path | None


def check(release_dir: Path, qemu_dir: Path | None, logs: Path) -> int:
    """Re-check an unpacked release: its files, images, descriptors and boots.

    The measured text modes must equal the manifest's.
    """
    manifest = cast("dict[str, object]", json.loads((release_dir / "manifest.json").read_text()))
    problems: list[str] = []
    if manifest.get("format") != FORMAT:
        problems.append(f"manifest format {manifest.get('format')!r}, not {FORMAT}")
    rel = str(manifest["release"])
    edk2 = cast("dict[str, object]", manifest["edk2"])
    tag, commit = str(edk2["tag"]), str(edk2["commit"])
    images = cast("dict[str, dict[str, object]]", manifest["images"])
    if sorted(images) != sorted(img.dir for img in IMAGES):
        problems.append(f"manifest images {sorted(images)}, not {sorted(img.dir for img in IMAGES)}")
    for img in IMAGES:
        entry = images.get(img.dir, {})
        for key in ("code", "vars", "shell"):
            f = cast("dict[str, object]", entry.get(key, {}))
            path = release_dir / str(f.get("path", ""))
            if not path.is_file() or path.stat().st_size != f.get("size") or sha256(path) != f.get("sha256"):
                problems.append(f"{path}: not the manifest's {key} (size or sha256)")
        if entry.get("flavour") != img.flavour or entry.get("arch") != img.arch:
            problems.append(f"{img.dir}: the manifest says {entry.get('arch')} {entry.get('flavour')}")
        name = release_dir / "descriptors" / img.descriptor(rel)
        try:
            got = json.loads(name.read_text())
        except (OSError, ValueError) as err:
            problems.append(f"{name}: {err}")
        else:
            if got != descriptor(img, rel, tag, commit, ""):
                problems.append(f"{name}: not the descriptor of {img.dir}")
        problems += checks.check_image(img.arch, release_dir / img.dir / "code.fd")
        problems += checks.check_shell(img.arch, release_dir / img.dir / img.shell)
    if problems:
        print("\n".join(f"FAIL: {p}" for p in problems), file=sys.stderr)
        return 1
    measured, problems = boot_checks(release_dir, tag, qemu_dir, logs)
    for img in IMAGES:
        want = cast("dict[str, object]", images[img.dir]["measured"]).get("text_modes")
        got = measured[img.dir].get("text_modes")
        if got != want:
            problems.append(f"{img.dir}: text modes measured {json.dumps(got)}, the manifest says {json.dumps(want)}")
    if problems:
        print("\n".join(f"FAIL: {p}" for p in problems), file=sys.stderr)
        print(f"(console transcripts in {logs})", file=sys.stderr)
        return 1
    print(f"{release_dir}: every check passed")
    return 0

