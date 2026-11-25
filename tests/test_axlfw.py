"""Unit tests of lib/axlfw: no build, no QEMU, no network.

Run by tests/firmware-tests.sh with the build's venv (uefi_firmware):
    python -m unittest discover -s tests -p 'test_*.py'
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

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


UIAPP_BYTES = "0x21,0xaa,0x2c,0x46,0x14,0x76,0x03,0x45,0x83,0x6e,0x8a,0xb6,0xf4,0x66,0x23,0x31"
MENU_APP_BYTES = "0xbf,0x3e,0x7d,0xee,0x8a,0xc6,0xf6,0x4a,0x96,0xef,0x43,0xa4,0x3f,0x37,0x40,0x9b"


def _build(root: Path, flavour: str, arch: str, mask: str, level: str, menu: str, ndebug: bool) -> Path:
    b = root / f"{flavour}_GCC5"
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
        tag, commit, rel = "edk2-stable202602", "b7a715f7c03c45c6b4575bf88596bfd79658b8ce", "edk2-stable202602-r1"
        want = json.loads((FIXTURES / "descriptors.json").read_text())
        got = {
            img.descriptor(rel): release.descriptor(img, rel, tag, commit, "/p/edk2-stable202602-r1/")
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


def _fake_release(
    root: Path, rel: str, corrupt: bool = False, fmt: int = 2, outside: bool = False, asset: str | None = None
) -> Path:
    """A small release tarball shaped as a real one, and its SHA256SUMS."""
    tag, commit = "edk2-stable202602", "b7a715f7c03c45c6b4575bf88596bfd79658b8ce"
    stage = Path(tempfile.mkdtemp(dir=root, prefix=f"stage-{rel}-"))
    top = stage / f"axl-firmware-{rel}"
    images: dict[str, object] = {}
    (top / "descriptors").mkdir(parents=True)
    for img in release.IMAGES:
        d = top / img.dir
        d.mkdir(parents=True)
        for name in ("code.fd", "vars.fd", img.shell):
            (d / name).write_bytes(f"{rel} {img.dir} {name}".encode())
        images[img.dir] = {
            "arch": img.arch,
            "flavour": img.flavour,
            "code": release.file_entry(top, f"{img.dir}/code.fd"),
            "vars": release.file_entry(top, f"{img.dir}/vars.fd"),
            "shell": release.file_entry(top, f"{img.dir}/{img.shell}"),
        }
        (top / "descriptors" / img.descriptor(rel)).write_text(
            json.dumps(release.descriptor(img, rel, tag, commit, ""))
        )
    if outside:
        d = top / "descriptors" / release.Image("x64", "DEBUG").descriptor(rel)
        bad = json.loads(d.read_text())
        bad["mapping"]["executable"]["filename"] = "../../etc/passwd"
        d.write_text(json.dumps(bad))
    manifest = {
        "format": fmt,
        "release": rel,
        "images": images,
        "descriptors": [f"descriptors/{img.descriptor(rel)}" for img in release.IMAGES],
    }
    (top / "manifest.json").write_text(json.dumps(manifest))
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
        r1 = _fake_release(self.root, "edk2-stable202602-r1")
        install.install("o/r", str(r1), self.prefix, None, symbols=False)
        rel = "edk2-stable202602-r1"
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
                release.Image("x64", "RELEASE"), rel, "edk2-stable202602",
                "b7a715f7c03c45c6b4575bf88596bfd79658b8ce", f"{self.prefix.resolve()}/{rel}/",
            ),
        )
        self.assertEqual((self.prefix / rel / "release/x64/code.fd").read_bytes(), f"{rel} release/x64 code.fd".encode())
        # Another release: its descriptors replace these; r1's files stay.
        unrelated = self.prefix / "descriptors" / "50-other.json"
        unrelated.write_text("{}")
        r2 = _fake_release(self.root, "edk2-stable202602-r2")
        install.install("o/r", str(r2), self.prefix, None, symbols=False)
        self.assertEqual(
            self._descriptors(),
            [
                "10-axl-firmware-edk2-stable202602-r2-aarch64.json",
                "10-axl-firmware-edk2-stable202602-r2-x64.json",
                "20-axl-firmware-edk2-stable202602-r2-release-aarch64.json",
                "20-axl-firmware-edk2-stable202602-r2-release-x64.json",
                "50-other.json",
            ],
        )
        self.assertTrue((self.prefix / rel / "x64/code.fd").is_file())
        # The same release again replaces its directory whole.
        install.install("o/r", str(r2), self.prefix, None, symbols=False)
        self.assertEqual(sorted(p.name for p in self.prefix.iterdir()), sorted(
            [".lock", "descriptors", "edk2-stable202602-r1", "edk2-stable202602-r2"]))

    def test_wrong_digest(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202602-r1")
        got = hashlib.sha256(r1.read_bytes()).hexdigest()
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(r1), self.prefix, "0" * 64, symbols=False)
        self.assertEqual(str(cm.exception), f"{r1}: sha256 {got}, expected {'0' * 64}")
        self.assertEqual(os.listdir(self.prefix), [".lock"])
        install.install("o/r", str(r1), self.prefix, got, symbols=False)

    def test_refusals_leave_the_install_as_it_was(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202602-r1")
        install.install("o/r", str(r1), self.prefix, None, symbols=False)
        before = sorted(str(p.relative_to(self.prefix)) for p in self.prefix.rglob("*"))
        top = "axl-firmware-edk2-stable202602-r1"
        cases = {
            "format": (_fake_release(self.root, "edk2-stable202602-r1", fmt=1),
                       f"manifest format 1; this install reads format 2"),
            "outside": (_fake_release(self.root, "edk2-stable202602-r1", outside=True),
                        "the manifest names '../../etc/passwd', outside the release"),
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
        bad = _fake_release(self.root, "edk2-stable202602-r1", asset="axl-firmware-...tar.xz")
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(bad), self.prefix, None, symbols=False)
        self.assertEqual(
            str(cm.exception),
            f"{bad}: not named axl-firmware-<release>.tar.xz, with a release edk2-stableYYYYMM-rN",
        )

    def test_no_sums(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202602-r1")
        (r1.parent / "SHA256SUMS").unlink()
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(r1), self.prefix, None, symbols=False)
        self.assertEqual(str(cm.exception), f"{r1}: no SHA256SUMS beside it; give its sha256 with --sha256")

    def test_corrupt_set(self) -> None:
        r1 = _fake_release(self.root, "edk2-stable202602-r1", corrupt=True)
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(r1), self.prefix, None, symbols=False)
        self.assertRegex(str(cm.exception), r"/axl-firmware-edk2-stable202602-r1/x64/code\.fd: not x64's code as its manifest records it$")
        self.assertEqual(os.listdir(self.prefix), [".lock"])

    def test_member_outside(self) -> None:
        for name in ("../escape", "axl-firmware-edk2-stable202602-r9/../../escape", "elsewhere/file"):
            with self.subTest(name):
                bad = self.root / "axl-firmware-edk2-stable202602-r9.tar.xz"
                with tarfile.open(bad, "w:xz") as tar:
                    tar.addfile(tarfile.TarInfo(name))
                digest = hashlib.sha256(bad.read_bytes()).hexdigest()
                with self.assertRaises(install.InstallError) as cm:
                    install.install("o/r", str(bad), self.prefix, digest, symbols=False)
                self.assertEqual(str(cm.exception), f"{bad}: unexpected member {name!r}")
                self.assertFalse((self.root / "escape").exists())

    def test_not_a_tag(self) -> None:
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", "latest", self.prefix, None, symbols=False)
        self.assertEqual(
            str(cm.exception), "--from latest: no such file, and not a release tag (edk2-stableYYYYMM-rN)"
        )


if __name__ == "__main__":
    unittest.main()
