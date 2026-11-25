"""Unit tests of lib/axlfw: no build, no QEMU, no network.

Run by tests/firmware-tests.sh with the build's venv (uefi_firmware):
    python -m unittest discover -s tests -p 'test_*.py'
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from collections.abc import Callable
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from typing import cast  # noqa: E402

from axlfw import boot, checks, fv, install, release  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FS = "7BD9DDF7-8B83-488E-AEC9-24C78610289C"
GPU = "D6099B94-CD97-4CC5-8714-7F6312701A8A"
SHELL = "7C04A583-9E3E-4F1C-AD65-E05268D0B4D1"
DXE_CORE = "D6A2CB7F-6A18-4E2F-B43B-9920A733700A"
DXE_FV = "9E21FD93-9C72-4C15-8C4B-E77F1DB2D792"


class WalkerTests(unittest.TestCase):
    """The FV walker on axlvmpool's stand-in images."""

    def test_each_stand_in(self) -> None:
        want = {
            "both.fd": {FS, GPU},
            "both-lzma.fd": {FS, GPU, SHELL, DXE_CORE, DXE_FV},
            "no-virtiofs.fd": {GPU, SHELL, DXE_CORE, DXE_FV},
            "no-gpu.fd": {FS, SHELL, DXE_CORE, DXE_FV},
            "neither.fd": {SHELL, DXE_CORE, DXE_FV},
        }
        for name, guids in want.items():
            with self.subTest(name):
                self.assertEqual(fv.image_guids(FIXTURES / "volumes" / name), guids)

    def test_no_volume(self) -> None:
        with self.assertRaises(fv.NotAnImageError) as cm:
            fv.image_guids(FIXTURES / "volumes" / "not-a-volume.fd")
        self.assertEqual(str(cm.exception), "no firmware volume found")

    def test_check_image_names_what_is_present_and_must_not_be(self) -> None:
        image = FIXTURES / "volumes" / "both.fd"
        forbidden = (checks.Driver("VirtioGpuDxe", GPU, "x"), checks.Driver("Nowhere", "0" * 8 + "-0000-0000-0000-" + "0" * 12, "y"))
        with mock.patch.object(checks, "ABSENT", forbidden), mock.patch.object(checks, "COMMON_DRIVERS", ()):
            self.assertEqual(checks.check_image("aarch64", image), [f"{image} holds: VirtioGpuDxe ({GPU})"])

    def test_check_image_names_what_is_missing(self) -> None:
        image = FIXTURES / "volumes" / "no-gpu.fd"
        missing = [d for d in checks.drivers("aarch64") if d.guid not in {FS, SHELL}]
        self.assertEqual(
            checks.check_image("aarch64", image),
            [f"{image} lacks: " + ", ".join(f"{d.name} ({d.guid})" for d in missing)],
        )


def _pe(machine: int, magic: int = 0x20B, subsystem: int = 10) -> bytes:
    data = bytearray(0x200)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    struct.pack_into("<H", data, 0x80 + 24, magic)
    struct.pack_into("<H", data, 0x80 + 24 + 68, subsystem)
    return bytes(data)


class ShellTests(unittest.TestCase):
    """The Shell's PE checks."""

    def test_shells(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "Shell.efi"
            p.write_bytes(_pe(0x8664))
            self.assertEqual(checks.check_shell("x64", p), [])
            self.assertEqual(checks.check_shell("aarch64", p), [f"{p}: machine 0x8664, not aarch64's 0xaa64"])
            p.write_bytes(_pe(0xAA64, magic=0x10B, subsystem=11))
            self.assertEqual(
                checks.check_shell("aarch64", p),
                [f"{p}: not PE32+ (magic 0x10b)", f"{p}: subsystem 11, not an EFI application (10)"],
            )
            p.write_bytes(b"text")
            self.assertEqual(checks.check_shell("x64", p), [f"{p}: not a PE image"])

    def test_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "Shell.efi"
            # The PE signature is there; the optional header is cut short.
            p.write_bytes(_pe(0x8664)[:0x90])
            self.assertEqual(checks.check_shell("x64", p), [f"{p}: a truncated PE header (144 bytes)"])


UIAPP_BYTES = "0x21,0xaa,0x2c,0x46,0x14,0x76,0x03,0x45,0x83,0x6e,0x8a,0xb6,0xf4,0x66,0x23,0x31"
MENU_APP_BYTES = "0xbf,0x3e,0x7d,0xee,0x8a,0xc6,0xf6,0x4a,0x96,0xef,0x43,0xa4,0x3f,0x37,0x40,0x9b"


def _build(root: Path, flavour: str, arch: str, mask: str, level: str, menu: str, ndebug: bool) -> Path:
    b = root / f"{flavour}_GCC"
    a = "X64" if arch == "x64" else "AARCH64"
    dxe = b / a / "MdeModulePkg/Core/Dxe/DxeMain/DEBUG"
    bds = b / a / "MdeModulePkg/Universal/BdsDxe/BdsDxe/DEBUG"
    dxe.mkdir(parents=True)
    bds.mkdir(parents=True)
    (dxe / "AutoGen.c").write_text(
        f"#define _PCD_VALUE_PcdDebugPrintErrorLevel  {level}U\n#define _PCD_VALUE_PcdDebugPropertyMask  {mask}U\n"
    )
    (dxe.parent / "GNUmakefile").write_text("CC_FLAGS = -Os" + (" -DMDEPKG_NDEBUG\n" if ndebug else "\n"))
    (bds / "AutoGen.h").write_text(
        f"GLOBAL_REMOVE_IF_UNREFERENCED const UINT8 _gPcd_FixedAtBuild_PcdBootManagerMenuFile[16] = {{{menu}}};\n"
    )
    return b


class BuildFactsTests(unittest.TestCase):
    """What a build's AutoGen must say."""

    def test_debug_x64_qualifies(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "DEBUG", "x64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=False)
            self.assertEqual(
                checks.build_facts(b, "x64", "DEBUG"),
                (
                    {
                        "target": "DEBUG",
                        "asserts_compiled": True,
                        "property_mask": "0x2f",
                        "print_error_level": "0x8000004f",
                        "boot_manager_menu": "UiApp",
                        "assert": "deadloop",
                        "clear_memory": True,
                    },
                    [],
                ),
            )

    def test_debug_x64_without_clear_memory(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "DEBUG", "x64", "0x27", "0x8000004F", UIAPP_BYTES, ndebug=False)
            dxe = b / "X64/MdeModulePkg/Core/Dxe/DxeMain/DEBUG"
            self.assertEqual(
                checks.build_facts(b, "x64", "DEBUG")[1],
                [f"{dxe}: PcdDebugPropertyMask 0x27 lacks ASSERT (0x01 with 0x10 or 0x20) or CLEAR_MEMORY (0x08)"],
            )

    def test_boot_manager_menu_app(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "DEBUG", "aarch64", "0x2F", "0x80000000", MENU_APP_BYTES, ndebug=False)
            bds = b / "AARCH64/MdeModulePkg/Universal/BdsDxe/BdsDxe/DEBUG"
            self.assertEqual(
                checks.build_facts(b, "aarch64", "DEBUG")[1],
                [f"{bds}: PcdBootManagerMenuFile is {MENU_APP_BYTES}, not UiApp's {checks.UIAPP}"],
            )

    def test_aarch64_print_level(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "DEBUG", "aarch64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=False)
            dxe = b / "AARCH64/MdeModulePkg/Core/Dxe/DxeMain/DEBUG"
            self.assertEqual(
                checks.build_facts(b, "aarch64", "DEBUG")[1],
                [f"{dxe}: PcdDebugPrintErrorLevel 0x8000004f, not 0x80000000"],
            )

    def test_release_needs_ndebug(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "RELEASE", "x64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=False)
            mk = b / "X64/MdeModulePkg/Core/Dxe/DxeMain/GNUmakefile"
            self.assertEqual(
                checks.build_facts(b, "x64", "RELEASE")[1], [f"{mk}: a RELEASE build without MDEPKG_NDEBUG"]
            )
            mk.unlink()
            self.assertEqual(
                checks.build_facts(b, "x64", "RELEASE"), ({}, [f"{mk}: missing, so the build's flags cannot be read"])
            )

    def test_release_without_debug_pcds(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "RELEASE", "aarch64", "0x2F", "0x80000000", UIAPP_BYTES, ndebug=True)
            (b / "AARCH64/MdeModulePkg/Core/Dxe/DxeMain/DEBUG/AutoGen.c").write_text("/* DebugLibNull */\n")
            self.assertEqual(
                checks.build_facts(b, "aarch64", "RELEASE"),
                (
                    {
                        "target": "RELEASE",
                        "asserts_compiled": False,
                        "property_mask": None,
                        "print_error_level": None,
                        "boot_manager_menu": "UiApp",
                    },
                    [],
                ),
            )
            debug = b.parent / "DEBUG_GCC"
            b.rename(debug)
            self.assertEqual(
                checks.build_facts(debug, "aarch64", "DEBUG"),
                ({}, [f"{debug / 'AARCH64/MdeModulePkg/Core/Dxe/DxeMain/DEBUG'}: no AutoGen with the debug PCDs"]),
            )

    def test_flavour_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "RELEASE", "x64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=True)
            mk = b / "X64/MdeModulePkg/Core/Dxe/DxeMain/GNUmakefile"
            self.assertEqual(
                checks.build_facts(b, "x64", "DEBUG")[1],
                [f"{b}: not a DEBUG build", f"{mk}: a DEBUG build with MDEPKG_NDEBUG"],
            )


class DescriptorTests(unittest.TestCase):
    """The descriptors, exactly."""

    def test_each(self) -> None:
        tag, commit, rel = "edk2-stable202611", "556dcfb466e9f05affeaff881d738807f8156d3c", "edk2-stable202611-r1"
        want = json.loads((FIXTURES / "descriptors.json").read_text())
        got = {
            img.descriptor(rel): release.descriptor(img, rel, tag, commit, "/p/edk2-stable202611-r1/")
            for img in release.IMAGES
        }
        self.assertEqual(got, want)


class BootParseTests(unittest.TestCase):
    """Reading the Shell's output."""

    def test_mode(self) -> None:
        out = (
            "Available modes for console output device.\n"
            "  Col    80 Row    25   \n  Col    80 Row    50   \n  Col   240 Row    56  *\n"
        )
        self.assertEqual(boot.text_modes(out), {"modes": [[80, 25], [80, 50], [240, 56]], "current": [240, 56]})
        with self.assertRaises(boot.BootError):
            boot.text_modes("'mode' is not recognized\n")

    def test_screendump(self) -> None:
        black = b"P6\n4 2\n255\n" + b"\0" * 24
        self.assertEqual(boot.parse_ppm(black), (4, 2, False))
        self.assertEqual(boot.parse_ppm(black[:-1] + b"\x01"), (4, 2, True))
        self.assertIsNone(boot.parse_ppm(black[:-3]))
        self.assertIsNone(boot.parse_ppm(b"P5\n4 2\n255\n" + b"\0" * 8))

    def test_dmpstore(self) -> None:
        out = (
            "Variable NV+BS '7F1E3A52-6B0D-4C8E-9A27-5D4B1C3E2F60:AxlFirmwareNvCheck' DataSize = 0x08\n"
            "  00000000: 0A 1B 2C 3D 4E 5F 60 71                          *..,=N_`q*\n"
        )
        self.assertEqual(boot.hex_bytes(out), "0a1b2c3d4e5f6071")
        self.assertEqual(boot.hex_bytes("dmpstore: No matching variables found.\n"), "")


class GuestCloseTests(unittest.TestCase):
    """Closing a guest always stops its process and closes its files."""

    def test_monitor_fails(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            # A stand-in for QEMU that would run for a minute; the extra
            # arguments (the monitor's) are ignored.
            argv = [sys.executable, "-c", "import time; time.sleep(60)"]
            g = boot.Guest(argv, Path(t), Path(t), "fake", 5)
            self.addCleanup(lambda: g.proc.poll() is None and g.proc.kill())
            with mock.patch.object(g, "monitor", side_effect=boot.BootError("fake: the monitor failed")):
                g.close()
            self.assertIsNotNone(g.proc.returncode)
            self.assertTrue(g._stderr.closed)  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(g._reader.is_alive())  # pyright: ignore[reportPrivateUsage]


class BootChecksTests(unittest.TestCase):
    """The boot checks refuse a bad setting or a missing QEMU, said."""

    def test_settings(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            for var, value, want in (
                ("AXLFW_BOOT_JOBS", "0", "AXLFW_BOOT_JOBS='0': not a positive whole number"),
                ("AXLFW_BOOT_JOBS", "two", "AXLFW_BOOT_JOBS='two': not a positive whole number"),
                ("AXLFW_BOOT_TIMEOUT", "-1", "AXLFW_BOOT_TIMEOUT='-1': not a positive number of seconds"),
            ):
                qemu = mock.patch.object(boot.shutil, "which", return_value="/usr/bin/qemu")
                with self.subTest(var=var, value=value), mock.patch.dict(os.environ, {var: value}), qemu:
                    self.assertEqual(release.boot_checks(Path(t), "tag", None, Path(t) / "logs"), ({}, [want]))

    def test_no_qemu(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            empty = Path(t)
            self.assertEqual(
                release.boot_checks(empty, "tag", empty, empty / "logs"),
                (
                    {},
                    [
                        f"{empty}: no executable qemu-system-x86_64 (QEMU_DIR, or --qemu-dir)",
                        f"{empty}: no executable qemu-system-aarch64 (QEMU_DIR, or --qemu-dir)",
                    ],
                ),
            )
            with mock.patch.object(boot.shutil, "which", return_value=None):
                self.assertEqual(
                    release.boot_checks(empty, "tag", None, empty / "logs")[1],
                    [
                        "qemu-system-x86_64 not found on PATH (or set QEMU_DIR)",
                        "qemu-system-aarch64 not found on PATH (or set QEMU_DIR)",
                    ],
                )


class SymbolsTests(unittest.TestCase):
    """The symbols check."""

    def test_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "FV").mkdir()
            for name in release.SYMBOLS_REQUIRED:
                (d / name).write_bytes(b"\x7fELF" + b"\0" * 12)
            (d / "FV" / "DXEFV.Fv.map").write_text("map\n")
            self.assertEqual(release.check_symbols(d), [])
            (d / "Shell.debug").write_bytes(b"MZ")
            (d / "DxeCore.debug").unlink()
            (d / "FV" / "DXEFV.Fv.map").unlink()
            self.assertEqual(
                release.check_symbols(d),
                [f"{d}: no DxeCore.debug", f"{d / 'Shell.debug'}: not an ELF file", f"{d}/FV: no firmware-volume maps"],
            )


Manifest = dict[str, object]


def _fake_tree(
    root: Path,
    rel: str,
    fmt: int = 2,
    outside: bool = False,
    marker: str = "",
    tamper: Callable[[Manifest], None] | None = None,
    swap_descriptor: bool = False,
) -> Path:
    """A small unpacked release shaped as a real one; its top directory.

    tamper edits the manifest before it is written.
    """
    tag, commit = "edk2-stable202611", "556dcfb466e9f05affeaff881d738807f8156d3c"
    stage = Path(tempfile.mkdtemp(dir=root, prefix=f"stage-{rel}-"))
    top = stage / f"axl-firmware-{rel}"
    images: dict[str, object] = {}
    (top / "descriptors").mkdir(parents=True)
    for img in release.IMAGES:
        d = top / img.dir
        d.mkdir(parents=True)
        for name in ("code.fd", "vars.fd", img.shell):
            (d / name).write_bytes(f"{rel} {img.dir} {name}{marker}".encode())
        images[img.dir] = {
            "arch": img.arch,
            "flavour": img.flavour,
            "code": release.file_entry(top, f"{img.dir}/code.fd"),
            "vars": release.file_entry(top, f"{img.dir}/vars.fd"),
            "shell": release.file_entry(top, f"{img.dir}/{img.shell}"),
            "measured": {},
        }
        (top / "descriptors" / img.descriptor(rel)).write_text(
            json.dumps(release.descriptor(img, rel, tag, commit, ""))
        )
    if swap_descriptor:
        d = top / "descriptors" / release.Image("x64", "DEBUG").descriptor(rel)
        bad = json.loads(d.read_text())
        bad["mapping"]["executable"]["filename"] = "release/aarch64/code.fd"
        d.write_text(json.dumps(bad))
    if outside:
        d = top / "descriptors" / release.Image("x64", "DEBUG").descriptor(rel)
        bad = json.loads(d.read_text())
        bad["mapping"]["executable"]["filename"] = "../../etc/passwd"
        d.write_text(json.dumps(bad))
    manifest: Manifest = {
        "format": fmt,
        "release": rel,
        "edk2": {"tag": tag, "commit": commit},
        "images": images,
        "descriptors": [f"descriptors/{img.descriptor(rel)}" for img in release.IMAGES],
    }
    if tamper:
        tamper(manifest)
    (top / "manifest.json").write_text(json.dumps(manifest))
    return top


def _fake_release(
    root: Path,
    rel: str,
    corrupt: bool = False,
    fmt: int = 2,
    outside: bool = False,
    asset: str | None = None,
    marker: str = "",
    tamper: Callable[[Manifest], None] | None = None,
    swap_descriptor: bool = False,
    edit_tree: Callable[[Path], None] | None = None,
) -> Path:
    """A small release tarball shaped as a real one, and its SHA256SUMS."""
    top = _fake_tree(root, rel, fmt=fmt, outside=outside, marker=marker, tamper=tamper, swap_descriptor=swap_descriptor)
    if edit_tree:
        edit_tree(top)
    if corrupt:
        (top / "x64" / "code.fd").write_bytes(b"changed")
    dist = Path(tempfile.mkdtemp(dir=root, prefix=f"dist-{rel}-"))
    out = dist / (asset or f"axl-firmware-{rel}.tar.xz")
    release._tar(out, top, f"axl-firmware-{rel}", 0)  # pyright: ignore[reportPrivateUsage]
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (dist / "SHA256SUMS").write_text(f"{digest}  {out.name}\n")
    return out


class InstallTests(unittest.TestCase):
    """The install command, from local tarballs."""

    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.prefix = self.root / "prefix"

    def tearDown(self) -> None:
        self._t.cleanup()

    def _descriptors(self) -> list[str]:
        return sorted(p.name for p in (self.prefix / "descriptors").iterdir())

    def test_install_and_upgrade(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1")
        install.install("o/r", str(r1), self.prefix, None, symbols=False)
        rel = "edk2-stable202611-r1"
        self.assertEqual(
            self._descriptors(),
            [
                f"10-axl-firmware-{rel}-aarch64.json",
                f"10-axl-firmware-{rel}-x64.json",
                f"20-axl-firmware-{rel}-release-aarch64.json",
                f"20-axl-firmware-{rel}-release-x64.json",
            ],
        )
        got = json.loads((self.prefix / "descriptors" / f"20-axl-firmware-{rel}-release-x64.json").read_text())
        self.assertEqual(
            got,
            release.descriptor(
                release.Image("x64", "RELEASE"), rel, "edk2-stable202611",
                "556dcfb466e9f05affeaff881d738807f8156d3c", f"{self.prefix.resolve()}/{rel}/",
            ),
        )
        self.assertEqual((self.prefix / rel / "release/x64/code.fd").read_bytes(), f"{rel} release/x64 code.fd".encode())
        # Another release: its descriptors replace these; r1's files stay.
        unrelated = self.prefix / "descriptors" / "50-other.json"
        unrelated.write_text("{}")
        r2 = _fake_release(self.root, "edk2-stable202611-r2")
        install.install("o/r", str(r2), self.prefix, None, symbols=False)
        self.assertEqual(
            self._descriptors(),
            [
                "10-axl-firmware-edk2-stable202611-r2-aarch64.json",
                "10-axl-firmware-edk2-stable202611-r2-x64.json",
                "20-axl-firmware-edk2-stable202611-r2-release-aarch64.json",
                "20-axl-firmware-edk2-stable202611-r2-release-x64.json",
                "50-other.json",
            ],
        )
        self.assertTrue((self.prefix / rel / "x64/code.fd").is_file())
        # The same release again replaces its directory whole.
        install.install("o/r", str(r2), self.prefix, None, symbols=False)
        self.assertEqual(sorted(p.name for p in self.prefix.iterdir()), sorted(
            [".lock", "descriptors", "edk2-stable202611-r1", "edk2-stable202611-r2"]))

    def test_wrong_digest(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1")
        got = hashlib.sha256(r1.read_bytes()).hexdigest()
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(r1), self.prefix, "0" * 64, symbols=False)
        self.assertEqual(str(cm.exception), f"{r1}: sha256 {got}, expected {'0' * 64}")
        self.assertEqual(os.listdir(self.prefix), [".lock"])
        install.install("o/r", str(r1), self.prefix, got, symbols=False)

    def test_refusals_leave_the_install_as_it_was(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1")
        install.install("o/r", str(r1), self.prefix, None, symbols=False)
        before = sorted(str(p.relative_to(self.prefix)) for p in self.prefix.rglob("*"))
        top = "axl-firmware-edk2-stable202611-r1"
        cases = {
            "format": (_fake_release(self.root, "edk2-stable202611-r1", fmt=1),
                       f"manifest format 1; this install reads format 2"),
            "outside": (_fake_release(self.root, "edk2-stable202611-r1", outside=True),
                        "its executable is '../../etc/passwd', not 'x64/code.fd'"),
        }
        for label, (tarball, tail) in cases.items():
            with self.subTest(label):
                with self.assertRaises(install.InstallError) as cm:
                    install.install("o/r", str(tarball), self.prefix, None, symbols=False)
                self.assertTrue(str(cm.exception).endswith(tail), str(cm.exception))
                self.assertIn(f"/{top}", str(cm.exception))
                after = sorted(str(p.relative_to(self.prefix)) for p in self.prefix.rglob("*"))
                self.assertEqual(after, before)

    def test_bad_release_name(self) -> None:
        bad = _fake_release(self.root, "edk2-stable202611-r1", asset="axl-firmware-...tar.xz")
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(bad), self.prefix, None, symbols=False)
        self.assertEqual(
            str(cm.exception),
            f"{bad}: not named axl-firmware-<release>.tar.xz, with a release edk2-stableYYYYMM-rN",
        )

    def test_no_sums(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1")
        (r1.parent / "SHA256SUMS").unlink()
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(r1), self.prefix, None, symbols=False)
        self.assertEqual(str(cm.exception), f"{r1}: no SHA256SUMS beside it; give its sha256 with --sha256")

    def test_corrupt_set(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1", corrupt=True)
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(r1), self.prefix, None, symbols=False)
        self.assertRegex(str(cm.exception), r"/axl-firmware-edk2-stable202611-r1/x64/code\.fd: not x64's code as its manifest records it$")
        self.assertEqual(os.listdir(self.prefix), [".lock"])

    def test_member_outside(self) -> None:
        for name in ("../escape", "axl-firmware-edk2-stable202611-r9/../../escape", "elsewhere/file"):
            with self.subTest(name):
                bad = self.root / "axl-firmware-edk2-stable202611-r9.tar.xz"
                with tarfile.open(bad, "w:xz") as tar:
                    tar.addfile(tarfile.TarInfo(name))
                digest = hashlib.sha256(bad.read_bytes()).hexdigest()
                with self.assertRaises(install.InstallError) as cm:
                    install.install("o/r", str(bad), self.prefix, digest, symbols=False)
                self.assertEqual(str(cm.exception), f"{bad}: unexpected member {name!r}")
                self.assertFalse((self.root / "escape").exists())

    def test_tampered_manifests(self) -> None:
        def moved(m: Manifest) -> None:
            # x64's code entry names release/x64's file, its size and sha256
            # true of that file: only the path betrays it.
            images = cast("dict[str, dict[str, object]]", m["images"])
            images["x64"]["code"] = images["release/x64"]["code"]

        def partial(m: Manifest) -> None:
            images = cast("dict[str, object]", m["images"])
            del images["release/aarch64"]

        def empty(m: Manifest) -> None:
            m["images"] = {}

        def no_descriptors(m: Manifest) -> None:
            m["descriptors"] = []

        want = ["aarch64", "release/aarch64", "release/x64", "x64"]
        rel = "edk2-stable202611-r1"
        names = sorted(f"descriptors/{release.Image(a, f).descriptor(rel)}" for f in ("DEBUG", "RELEASE") for a in ("x64", "aarch64"))
        cases: dict[str, tuple[Callable[[Manifest], None], str]] = {
            "no descriptors": (no_descriptors, f"its manifest's descriptors are [], not {names}"),
            "moved": (moved, "its manifest names 'release/x64/code.fd' for x64's code, not 'x64/code.fd'"),
            "partial": (partial, f"its manifest's images are {want[:1] + want[2:]}, not {want}"),
            "empty": (empty, f"its manifest's images are [], not {want}"),
        }
        for label, (tamper, tail) in cases.items():
            with self.subTest(label):
                bad = _fake_release(self.root, "edk2-stable202611-r1", tamper=tamper)
                with self.assertRaises(install.InstallError) as cm:
                    install.install("o/r", str(bad), self.prefix, None, symbols=False)
                self.assertTrue(str(cm.exception).endswith(f"/axl-firmware-edk2-stable202611-r1: {tail}"), str(cm.exception))
                self.assertEqual(os.listdir(self.prefix), [".lock"])

    def test_descriptor_naming_another_set(self) -> None:
        bad = _fake_release(self.root, "edk2-stable202611-r1", swap_descriptor=True)
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(bad), self.prefix, None, symbols=False)
        self.assertTrue(
            str(cm.exception).endswith(
                "/descriptors/10-axl-firmware-edk2-stable202611-r1-x64.json:"
                " its executable is 'release/aarch64/code.fd', not 'x64/code.fd'"
            ),
            str(cm.exception),
        )
        self.assertEqual(os.listdir(self.prefix), [".lock"])

    def test_descriptor_for_another_arch(self) -> None:
        def retarget(top: Path) -> None:
            d = top / "descriptors" / release.Image("x64", "DEBUG").descriptor("edk2-stable202611-r1")
            bad = json.loads(d.read_text())
            bad["targets"][0]["architecture"] = "aarch64"
            d.write_text(json.dumps(bad))

        bad = _fake_release(self.root, "edk2-stable202611-r1", edit_tree=retarget)
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(bad), self.prefix, None, symbols=False)
        self.assertTrue(
            str(cm.exception).endswith("/descriptors/10-axl-firmware-edk2-stable202611-r1-x64.json: not the descriptor of x64"),
            str(cm.exception),
        )
        self.assertEqual(os.listdir(self.prefix), [".lock"])

    def _fail_second_write(self) -> contextlib.AbstractContextManager[object]:
        real = install._write  # pyright: ignore[reportPrivateUsage]
        calls: list[Path] = []

        def write(path: Path, text: str) -> None:
            calls.append(path)
            if len(calls) == 2:
                msg = f"{path}: injected failure"
                raise OSError(msg)
            real(path, text)

        return mock.patch.object(install, "_write", side_effect=write)

    def test_failed_fresh_install_leaves_no_descriptor(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1")
        with self._fail_second_write(), self.assertRaises(OSError):
            install.install("o/r", str(r1), self.prefix, None, symbols=False)
        self.assertEqual(self._descriptors(), [])
        self.assertEqual(sorted(os.listdir(self.prefix)), [".lock", "descriptors"])

    def test_failed_upgrade_leaves_the_earlier_install(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1")
        install.install("o/r", str(r1), self.prefix, None, symbols=False)
        # A descriptor edited since: a failed install of the same release puts
        # it back as it was, not as the install would have written it.
        edited = self.prefix / "descriptors" / "10-axl-firmware-edk2-stable202611-r1-x64.json"
        edited.write_text(edited.read_text().replace("DEBUG", "DEBUG, edited"))
        before = {p.name: p.read_text() for p in (self.prefix / "descriptors").iterdir()}
        for rel in ("edk2-stable202611-r2", "edk2-stable202611-r1"):
            with self.subTest(rel):
                again = _fake_release(self.root, rel)
                with self._fail_second_write(), self.assertRaises(OSError):
                    install.install("o/r", str(again), self.prefix, None, symbols=False)
                after = {p.name: p.read_text() for p in (self.prefix / "descriptors").iterdir()}
                self.assertEqual(after, before)
                self.assertEqual(
                    sorted(os.listdir(self.prefix)), [".lock", "descriptors", "edk2-stable202611-r1"]
                )

    def test_local_tarball_swapped_after_its_check(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1")
        evil = _fake_release(self.root, "edk2-stable202611-r1", marker=" evil")
        real = install._sha256  # pyright: ignore[reportPrivateUsage]

        def sha256_then_swap(path: Path) -> str:
            got = real(path)
            if path.name == r1.name:
                # Once its digest is taken, the file by that name changes.
                shutil.copyfile(evil, r1)
            return got

        with mock.patch.object(install, "_sha256", side_effect=sha256_then_swap):
            install.install("o/r", str(r1), self.prefix, None, symbols=False)
        rel = "edk2-stable202611-r1"
        self.assertEqual((self.prefix / rel / "x64/code.fd").read_bytes(), f"{rel} x64 code.fd".encode())

    def test_symbols_need_sums(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202611-r1")
        digest = hashlib.sha256(r1.read_bytes()).hexdigest()
        (r1.parent / "SHA256SUMS").unlink()
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(r1), self.prefix, digest, symbols=True)
        self.assertEqual(
            str(cm.exception),
            f"{r1}: --symbols needs SHA256SUMS beside it, for the symbols tarballs' digests"
            " (--sha256 gives only this tarball's)",
        )
        self.assertEqual(os.listdir(self.prefix), [".lock"])

    def test_not_a_tag(self) -> None:
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", "latest", self.prefix, None, symbols=False)
        self.assertEqual(
            str(cm.exception), "--from latest: no such file, and not a release tag (edk2-stableYYYYMM-rN)"
        )


class ReleaseCheckTests(unittest.TestCase):
    """`build.sh check` on tampered manifests: each refused, said."""

    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        # The images are stand-ins: only the manifest is under test.
        for name in ("check_image", "check_shell"):
            p = mock.patch.object(checks, name, return_value=[])
            p.start()
            self.addCleanup(p.stop)
        boots = mock.patch.object(release, "boot_checks", return_value=({img.dir: {} for img in release.IMAGES}, []))
        boots.start()
        self.addCleanup(boots.stop)

    def tearDown(self) -> None:
        self._t.cleanup()

    def _check(self, top: Path) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = release.check(top, None, self.root / "logs")
        return rc, err.getvalue()

    def test_untampered(self) -> None:
        self.assertEqual(self._check(_fake_tree(self.root, "edk2-stable202611-r1")), (0, ""))

    def test_one_layout(self) -> None:
        # The install checks the image sets the assembly writes.
        self.assertEqual(sorted(img.dir for img in release.IMAGES), sorted(install.IMAGE_DIRS))
        self.assertEqual(
            [img.files for img in release.IMAGES][:1],
            [{"code": "x64/code.fd", "vars": "x64/vars.fd", "shell": "x64/Shell_X64.efi"}],
        )
        self.assertEqual(release.Image("aarch64", "RELEASE").shell, "Shell_AARCH64.efi")

    def test_tampered(self) -> None:
        def moved(m: Manifest) -> None:
            images = cast("dict[str, dict[str, object]]", m["images"])
            images["x64"]["code"] = images["release/x64"]["code"]

        def empty(m: Manifest) -> None:
            m["images"] = {}

        def no_edk2(m: Manifest) -> None:
            del m["edk2"]

        def no_code(m: Manifest) -> None:
            del cast("dict[str, dict[str, object]]", m["images"])["aarch64"]["code"]

        want = sorted(img.dir for img in release.IMAGES)
        cases: dict[str, tuple[Callable[[Manifest], None], str]] = {
            "moved": (moved, "FAIL: x64: the manifest's code is 'release/x64/code.fd', not 'x64/code.fd'\n"),
            "empty": (empty, f"FAIL: manifest images [], not {want}\n"),
            "no edk2": (no_edk2, "FAIL: {m}: no edk2.tag\n"),
            "no code": (no_code, "FAIL: aarch64: the manifest's code is None, not 'aarch64/code.fd'\n"),
        }
        for label, (tamper, text) in cases.items():
            with self.subTest(label):
                top = _fake_tree(self.root, "edk2-stable202611-r1", tamper=tamper)
                self.assertEqual(self._check(top), (1, text.format(m=top / "manifest.json")))

    def test_not_json(self) -> None:
        top = _fake_tree(self.root, "edk2-stable202611-r1")
        (top / "manifest.json").write_text("{")
        rc, err = self._check(top)
        self.assertEqual((rc, err.split(": ", 2)[:2]), (1, ["FAIL", str(top / "manifest.json")]))


class BuildShTests(unittest.TestCase):
    """build.sh's functions, sourced as the script runs them (set -euo pipefail)."""

    def test_repo_facts_outside_a_checkout(self) -> None:
        here = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as t:
            shutil.copyfile(here / "build.sh", Path(t) / "build.sh")
            r = subprocess.run(
                ["bash", "-c", '. "$1"; repo_facts; echo "$repo_commit $repo_dirty"', "-", f"{t}/build.sh"],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(t).parent)},
            )
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "none -1\n", ""))

    def _default_jobs(self, prefix: list[str], path: str) -> tuple[int, str, str]:
        here = Path(__file__).resolve().parent.parent
        r = subprocess.run(
            [*prefix, "bash", "-c", '. "$1"; default_jobs', "-", str(here / "build.sh")],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": path},
        )
        return r.returncode, r.stdout, r.stderr

    def test_default_jobs_is_the_cpus_this_process_may_use(self) -> None:
        taskset = shutil.which("taskset")
        if taskset is None:
            self.skipTest("no taskset")
        self.assertEqual(self._default_jobs([taskset, "-c", "0"], os.environ["PATH"]), (0, "1\n", ""))

    def test_default_jobs_is_4_without_nproc(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            # A PATH with bash and dirname but no nproc.
            for tool in ("bash", "dirname"):
                found = shutil.which(tool)
                assert found is not None
                (Path(t) / tool).symlink_to(found)
            self.assertEqual(self._default_jobs([], t), (0, "4\n", ""))


if __name__ == "__main__":
    unittest.main()
