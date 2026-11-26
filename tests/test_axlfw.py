"""Unit tests of lib/axlfw: no build, no QEMU, no network.

Run by tests/firmware-tests.sh with the build's venv (uefi_firmware):
    python -m unittest discover -s tests -p 'test_*.py'
"""

from __future__ import annotations

import contextlib
import email.message
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
from collections.abc import Callable
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from typing import cast  # noqa: E402

from axlfw import boot, checks, fv, guard, install, isa, licenses, netfs, release, tpm, volinfo  # noqa: E402

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


class DriverListTests(unittest.TestCase):
    """The drivers each arch's images must hold, exactly."""

    def test_each_arch(self) -> None:
        common = [
            "VirtioFsDxe", "VirtioGpuDxe", "VirtioRngDxe", "VirtioNetDxe", "VirtioBlkDxe", "VirtioScsiDxe",
            "VirtioSerialDxe", "Ip6Dxe", "HttpDxe", "TlsDxe", "IScsiDxe", "MnpDxe", "Ip4Dxe", "TcpDxe",
            "Dhcp4Dxe", "Tcg2Dxe", "UsbMouseDxe", "UsbMouseAbsolutePointerDxe", "QemuVideoDxe", "UiApp",
            "Shell", "PciSioSerialDxe", "VirtioInputDxe", "RamDiskDxe",
        ]  # fmt: skip
        self.assertEqual([d.name for d in checks.drivers("aarch64")], common)
        self.assertEqual([d.name for d in checks.drivers("x64")], [*common, "SnpDxe", "Ps2MouseDxe"])

    def test_input_and_ram_disk_on_both_arches(self) -> None:
        # virtio-tablet-pci's absolute pointer (and virtio-keyboard's and
        # virtio-mouse's input) is VirtioInputDxe's, built on both since
        # 202608; RamDiskDxe is built on both (OvmfPkgX64.fdf, and
        # ArmVirtQemuFvMain.fdf.inc), by one INF each.
        want = [
            checks.Driver("VirtioInputDxe", "F141B1E5-9C7C-44CC-AFAA-E87D7689B113", "OvmfPkg/VirtioInputDxe/VirtioInput.inf"),
            checks.Driver(
                "RamDiskDxe", "28A03FF4-12B3-4305-A417-BB1A4F94081E", "MdeModulePkg/Universal/Disk/RamDiskDxe/RamDiskDxe.inf"
            ),
        ]
        for arch in checks.ARCHES:
            with self.subTest(arch):
                self.assertEqual([d for d in checks.drivers(arch) if d.name in ("VirtioInputDxe", "RamDiskDxe")], want)

    def test_pci_serial_on_both_arches(self) -> None:
        # Patch 0005 adds it to ArmVirtQemu; OvmfPkgX64 always had it, by
        # the same INF and so the same FILE_GUID.
        pci = checks.Driver(
            "PciSioSerialDxe",
            "E2775B47-D453-4EE3-ADA7-391A1B05AC17",
            "MdeModulePkg/Bus/Pci/PciSioSerialDxe/PciSioSerialDxe.inf",
        )
        for arch in checks.ARCHES:
            with self.subTest(arch):
                self.assertEqual([d for d in checks.drivers(arch) if d.name == "PciSioSerialDxe"], [pci])


# --- r3 console: each patch applied and compiled (checks.check_patches) -----
_PATCH = (
    "Subject: [PATCH] a test patch\r\n"
    "\r\n"
    "---\r\n"
    "diff --git a/Pkg/Dxe/a.c b/Pkg/Dxe/a.c\n"
    "--- a/Pkg/Dxe/a.c\n"
    "+++ b/Pkg/Dxe/a.c\n"
    "@@ -1,2 +1,4 @@\n"
    " int a;\r\n"
    "+int added;\r\n"
    "+\r\n"
    " int b;\r\n"
    "diff --git a/Pkg/Include/k.h b/Pkg/Include/k.h\n"
    "--- a/Pkg/Include/k.h\n"
    "+++ b/Pkg/Include/k.h\n"
    "@@ -1 +1,2 @@\n"
    " #define A 1\n"
    "+#define B 2\n"
    "diff --git a/Pkg/Pkg.dsc b/Pkg/Pkg.dsc\n"
    "--- a/Pkg/Pkg.dsc\n"
    "+++ b/Pkg/Pkg.dsc\n"
    "@@ -1 +1,2 @@\n"
    "   Pkg/Dxe/Dxe.inf\n"
    "+  Pkg/More/More.inf\n"
)


class PatchCheckTests(unittest.TestCase):
    """Each required patch applied to the overlay, and its C compiled from there."""

    def tree(self, t: str) -> tuple[Path, Path, Path, list[Path]]:
        root = Path(t)
        ws, src = root / "ws", root / "src"
        for base, extra in ((ws / "overlay", True), (src, False)):
            (base / "Pkg/Dxe").mkdir(parents=True)
            (base / "Pkg/Include").mkdir(parents=True)
            (base / "Pkg/Dxe/a.c").write_bytes(b"int a;\r\n" + (b"int added;\r\n\r\n" if extra else b"") + b"int b;\r\n")
            (base / "Pkg/Include/k.h").write_text("#define A 1\n" + ("#define B 2\n" if extra else ""))
            (base / "Pkg/Pkg.dsc").write_text("  Pkg/Dxe/Dxe.inf\n" + ("  Pkg/More/More.inf\n" if extra else ""))
        patch = root / "0009-test.patch"
        patch.write_bytes(_PATCH.encode())
        out = ws / "Build/P/DEBUG_GCC/X64/Pkg/Dxe/Dxe/OUTPUT"
        out.mkdir(parents=True)
        (out / "a.obj.deps").write_text(
            f"{out}/a.obj:  {ws}/overlay/Pkg/Dxe/a.c \\\n {ws}/overlay/Pkg/Include/k.h \\\n {src}/Pkg/Include/z.h\n"
        )
        return ws, src, out, [patch]

    def check(self, ws: Path, src: Path, patches: list[Path], required: tuple[str, ...] = ("0009-test.patch",)) -> list[str]:
        return checks.check_patches(patches, required, ws, src, [ws / "Build/P/DEBUG_GCC/X64"])

    def test_passes(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ws, src, _, patches = self.tree(t)
            self.assertEqual(self.check(ws, src, patches), [])

    def test_patch_files(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            _, _, _, patches = self.tree(t)
            self.assertEqual(checks.patch_files(patches[0]), ["Pkg/Dxe/a.c", "Pkg/Include/k.h", "Pkg/Pkg.dsc"])

    def test_required_patch_missing(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ws, src, _, patches = self.tree(t)
            self.assertEqual(
                self.check(ws, src, patches, ("0009-test.patch", "0010-gone.patch")),
                ["0010-gone.patch: required, but the build did not apply it"],
            )

    def test_overlay_lacks_a_line(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ws, src, _, patches = self.tree(t)
            (ws / "overlay/Pkg/Pkg.dsc").write_text("  Pkg/Dxe/Dxe.inf\n")
            self.assertEqual(
                self.check(ws, src, patches),
                [f"0009-test.patch: {ws}/overlay/Pkg/Pkg.dsc is not the source's Pkg/Pkg.dsc with the patches applied"],
            )

    def test_does_not_apply_again(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ws, src, _, patches = self.tree(t)
            (src / "Pkg/Pkg.dsc").write_text("  Pkg/Other/Other.inf\n")
            got = self.check(ws, src, patches)
            self.assertEqual(len(got), 1)
            self.assertRegex(
                got[0], r"\A0009-test\.patch: does not apply to the source's files again: error: patch failed: Pkg/Pkg\.dsc:1"
            )

    def test_a_later_patch_changes_an_earlier_ones_line(self) -> None:
        # Stacked patches: the overlay is the source with both applied.
        with tempfile.TemporaryDirectory() as t:
            ws, src, _, patches = self.tree(t)
            later = Path(t) / "0010-later.patch"
            later.write_text(
                "diff --git a/Pkg/Include/k.h b/Pkg/Include/k.h\n"
                "--- a/Pkg/Include/k.h\n"
                "+++ b/Pkg/Include/k.h\n"
                "@@ -1,2 +1,2 @@\n"
                " #define A 1\n"
                "-#define B 2\n"
                "+#define B 3\n"
            )
            (ws / "overlay/Pkg/Include/k.h").write_text("#define A 1\n#define B 3\n")
            self.assertEqual(self.check(ws, src, [*patches, later], ("0009-test.patch", "0010-later.patch")), [])

    def test_compiled_from_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ws, src, out, patches = self.tree(t)
            (out / "b.obj.deps").write_text(f"{out}/b.obj:  {src}/Pkg/Dxe/a.c \\\n {src}/Pkg/Include/k.h\n")
            self.assertEqual(
                self.check(ws, src, patches),
                [
                    f"0009-test.patch: 1 objects compiled from the unpatched {src}/Pkg/Dxe/a.c, first {out}/b.obj.deps",
                    f"0009-test.patch: 1 objects compiled from the unpatched {src}/Pkg/Include/k.h, first {out}/b.obj.deps",
                ],
            )

    def test_not_compiled(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ws, src, out, patches = self.tree(t)
            (out / "a.obj.deps").write_text(f"{out}/a.obj:  {ws}/overlay/Pkg/Dxe/a.c\n")
            self.assertEqual(
                self.check(ws, src, patches),
                [f"0009-test.patch: no object compiled from the patched {ws}/overlay/Pkg/Include/k.h"],
            )

    def test_required_list(self) -> None:
        self.assertEqual(
            checks.REQUIRED_PATCHES,
            (
                "0001-pointer-drivers.patch",
                "0002-bochs-display-video.patch",
                "0003-map-debug-uart.patch",
                "0004-terminal-control-keys.patch",
                "0005-pci-serial.patch",
                "0006-ps2-mouse-sio.patch",
                "0007-shell-ctrl-s.patch",
                "0008-terminal-widechar.patch",
                "0009-shell-delete-absent.patch",
                "0010-virtio-mouse-wheel.patch",
                "0011-virtiofs-tpl.patch",
                "0012-localapic-eoi.patch",
                "0013-tcp-clone-child.patch",
                "0014-aarch64-tpm-pei-map.patch",
                "0015-reconnect-rng.patch",
            ),
        )

    def test_assemble_stops_on_a_patch_problem(self) -> None:
        # Checked with the static checks, before any boot, on every image's
        # arch directory.
        a = release.AssembleArgs(
            ws=Path("/nonexistent/ws"), src=Path("/nonexistent/src"), dist=Path("/nonexistent/dist"),
            stage=Path(tempfile.mkdtemp()) / "stage", release="r", tag="t", commit="c", url="u", fixes=[],
            toolchain="GCC", release_date="01/01/2026", compilers={}, defines={}, pcds={},
            patches=[Path("/p/0001-pointer-drivers.patch")], repo_commit="none", repo_dirty=0, built_by="local",
            qemu_dir=None,
        )  # fmt: skip
        self.addCleanup(shutil.rmtree, a.stage.parent)
        err = io.StringIO()
        with (
            mock.patch.object(release, "stage_images", return_value=[]),
            mock.patch.object(release, "static_checks", return_value=({}, [])),
            mock.patch.object(release, "volinfo_checks", return_value=[]),
            mock.patch.object(release, "isa_checks", return_value=({}, [])),
            mock.patch.object(checks, "build_facts", return_value=({}, [])),
            mock.patch.object(checks, "check_patches", return_value=["0007: no"]) as patched,
            mock.patch.object(release, "boot_checks", side_effect=AssertionError("booted")),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(release.assemble(a), 1)
        self.assertEqual(err.getvalue(), "FAIL: 0007: no\n")
        build = "/nonexistent/ws/Build"
        patched.assert_called_once_with(
            a.patches,
            checks.REQUIRED_PATCHES,
            a.ws,
            a.src,
            [
                Path(f"{build}/OvmfX64/DEBUG_GCC/X64"),
                Path(f"{build}/ArmVirtQemu-AArch64/DEBUG_GCC/AARCH64"),
                Path(f"{build}/OvmfX64/RELEASE_GCC/X64"),
                Path(f"{build}/ArmVirtQemu-AArch64/RELEASE_GCC/AARCH64"),
                Path("/nonexistent/ws/guarded/Build/OvmfX64/DEBUG_GCC/X64"),
                Path("/nonexistent/ws/guarded/Build/ArmVirtQemu-AArch64/DEBUG_GCC/AARCH64"),
            ],
        )

    def test_repository_patches_are_required(self) -> None:
        here = sorted(p.name for p in (Path(__file__).resolve().parent.parent / "patches").glob("*.patch"))
        self.assertEqual(sorted(checks.REQUIRED_PATCHES), here)


# --- end of r3 console: each patch applied and compiled ----------------------


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


NO_GUARD = ("0x0", "0x0ULL", "0x0ULL")
GUARD = ("0x03", "0x000000000000001EULL", "0x1EULL")


def _build(
    root: Path,
    flavour: str,
    arch: str,
    mask: str,
    level: str,
    menu: str,
    ndebug: bool,
    guard: tuple[str, ...] | None = NO_GUARD,
) -> Path:
    b = root / f"{flavour}_GCC"
    a = "X64" if arch == "x64" else "AARCH64"
    dxe = b / a / "MdeModulePkg/Core/Dxe/DxeMain/DEBUG"
    bds = b / a / "MdeModulePkg/Universal/BdsDxe/BdsDxe/DEBUG"
    dxe.mkdir(parents=True)
    bds.mkdir(parents=True)
    (dxe / "AutoGen.c").write_text(
        f"#define _PCD_VALUE_PcdDebugPrintErrorLevel  {level}U\n#define _PCD_VALUE_PcdDebugPropertyMask  {mask}U\n"
    )
    if guard is not None:
        # As AutoGen.h spells them: a UINT8 with U, the UINT64s with ULL.
        names = ("PcdHeapGuardPropertyMask", "PcdHeapGuardPoolType", "PcdHeapGuardPageType")
        (dxe / "AutoGen.h").write_text(
            "".join(f"#define _PCD_VALUE_{n}  {v}{'U' if n.endswith('Mask') else ''}\n" for n, v in zip(names, guard))
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

    def test_guarded_x64_qualifies(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "DEBUG", "x64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=False, guard=GUARD)
            self.assertEqual(
                checks.build_facts(b, "x64", "GUARDED"),
                (
                    {
                        "target": "DEBUG",
                        "asserts_compiled": True,
                        "property_mask": "0x2f",
                        "print_error_level": "0x8000004f",
                        "boot_manager_menu": "UiApp",
                        "heap_guard": {"property_mask": "0x03", "pool_type": "0x1e", "page_type": "0x1e"},
                        "assert": "deadloop",
                        "clear_memory": True,
                    },
                    [],
                ),
            )

    def test_heap_guard_values(self) -> None:
        self.assertEqual(
            checks.HEAP_GUARD,
            {"PcdHeapGuardPropertyMask": 0x03, "PcdHeapGuardPoolType": 0x1E, "PcdHeapGuardPageType": 0x1E},
        )

    def test_guarded_without_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "DEBUG", "aarch64", "0x2F", "0x80000000", UIAPP_BYTES, ndebug=False)
            dxe = b / "AARCH64/MdeModulePkg/Core/Dxe/DxeMain/DEBUG"
            self.assertEqual(
                checks.build_facts(b, "aarch64", "GUARDED")[1],
                [
                    f"{dxe}: the guarded image's PcdHeapGuardPropertyMask 0x00, not 0x03;"
                    " PcdHeapGuardPoolType 0x00, not 0x1e; PcdHeapGuardPageType 0x00, not 0x1e"
                ],
            )

    def test_guarded_with_the_freed_memory_guard(self) -> None:
        # BIT4 alone: the use-after-free guard, which excludes the pool guard.
        with tempfile.TemporaryDirectory() as t:
            b = _build(
                Path(t), "DEBUG", "x64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=False, guard=("0x10", "0x1E", "0x1E")
            )
            dxe = b / "X64/MdeModulePkg/Core/Dxe/DxeMain/DEBUG"
            self.assertEqual(
                checks.build_facts(b, "x64", "GUARDED")[1],
                [f"{dxe}: the guarded image's PcdHeapGuardPropertyMask 0x10, not 0x03"],
            )

    def test_guarded_is_a_debug_build(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "RELEASE", "x64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=True, guard=GUARD)
            mk = b / "X64/MdeModulePkg/Core/Dxe/DxeMain/GNUmakefile"
            self.assertEqual(
                checks.build_facts(b, "x64", "GUARDED")[1],
                [f"{b}: not a DEBUG build", f"{mk}: a DEBUG build with MDEPKG_NDEBUG"],
            )

    def test_heap_guard_only_on_guarded(self) -> None:
        for flavour, ndebug in (("DEBUG", False), ("RELEASE", True)):
            with self.subTest(flavour), tempfile.TemporaryDirectory() as t:
                b = _build(Path(t), flavour, "x64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=ndebug, guard=GUARD)
                dxe = b / "X64/MdeModulePkg/Core/Dxe/DxeMain/DEBUG"
                self.assertEqual(
                    checks.build_facts(b, "x64", flavour)[1],
                    [f"{dxe}: a {flavour} build with the heap guard (PcdHeapGuardPropertyMask 0x03)"],
                )

    def test_no_heap_guard_pcds(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            b = _build(Path(t), "DEBUG", "x64", "0x2F", "0x8000004F", UIAPP_BYTES, ndebug=False, guard=None)
            dxe = b / "X64/MdeModulePkg/Core/Dxe/DxeMain/DEBUG"
            self.assertEqual(
                checks.build_facts(b, "x64", "DEBUG")[1],
                [f"{dxe}: no AutoGen value for PcdHeapGuardPropertyMask, PcdHeapGuardPoolType, PcdHeapGuardPageType"],
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
            for img in release.DESCRIBED
        }
        self.assertEqual(got, want)

    def test_guarded_has_none(self) -> None:
        self.assertEqual(
            [(img.dir, img.flavour, img.described) for img in release.IMAGES],
            [
                ("x64", "DEBUG", True),
                ("aarch64", "DEBUG", True),
                ("release/x64", "RELEASE", True),
                ("release/aarch64", "RELEASE", True),
                ("guarded/x64", "GUARDED", False),
                ("guarded/aarch64", "GUARDED", False),
            ],
        )
        self.assertEqual([img.dir for img in release.DESCRIBED], list(install.IMAGE_DIRS))
        with self.assertRaises(ValueError) as cm:
            release.Image("x64", "GUARDED").descriptor("edk2-stable202611-r3")
        self.assertEqual(str(cm.exception), "guarded/x64: no descriptor")

    def test_guarded_builds_apart(self) -> None:
        ws = Path("/w")
        self.assertEqual(
            [str(release.Image(a, f).build(ws, "GCC")) for f in checks.FLAVOURS for a in checks.ARCHES],
            [
                "/w/Build/OvmfX64/DEBUG_GCC",
                "/w/Build/ArmVirtQemu-AArch64/DEBUG_GCC",
                "/w/Build/OvmfX64/RELEASE_GCC",
                "/w/Build/ArmVirtQemu-AArch64/RELEASE_GCC",
                "/w/guarded/Build/OvmfX64/DEBUG_GCC",
                "/w/guarded/Build/ArmVirtQemu-AArch64/DEBUG_GCC",
            ],
        )
        self.assertEqual(release.Image("aarch64", "GUARDED").report(ws), Path("/w/report-AARCH64-GUARDED.txt"))
        self.assertEqual(release.Image("x64", "GUARDED").files, {
            "code": "guarded/x64/code.fd", "vars": "guarded/x64/vars.fd", "shell": "guarded/x64/Shell_X64.efi"})


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


class GuestStateTests(unittest.TestCase):
    """A boot that stops: QEMU's state is kept, from its monitor."""

    # HMP replies trimmed from QEMU 10.0.0's, behind an echo of the line.
    REPLIES = {
        "info status": "VM status: running\r\n",
        "info registers -a": "CPU#0\r\nRIP=000000003eef87ec RFL=00000246\r\nCPU#1\r\nRIP=000000003eef8800\r\n",
        "info virtio": (
            "/machine/peripheral-anon/device[0]/virtio-backend [virtio-blk]\r\n"
            "/machine/peripheral-anon/device[1]/virtio-backend [virtio-gpu]\r\n"
        ),
        "info virtio-status /machine/peripheral-anon/device[0]/virtio-backend": "  num_vqs:                 2\r\n",
        "info virtio-status /machine/peripheral-anon/device[1]/virtio-backend": "  num_vqs:                 2\r\n",
        "info virtio-queue-status /machine/peripheral-anon/device[0]/virtio-backend 0": (
            "  last_avail_idx:       9\r\n    avail:        0x000000003e840400\r\n"
            "    used:         0x000000003e841000\r\n"
        ),
        "info virtio-queue-status /machine/peripheral-anon/device[0]/virtio-backend 1": (
            "  last_avail_idx:       0\r\n    avail:        0x0000000000000000\r\n"
            "    used:         0x0000000000000000\r\n"
        ),
        "info virtio-queue-status /machine/peripheral-anon/device[1]/virtio-backend 0": (
            "  used_idx:             173\r\n  last_avail_idx:       173\r\n"
            "    avail:        0x000000003e84e400\r\n    used:         0x000000003e84f000\r\n"
        ),
        "info virtio-queue-status /machine/peripheral-anon/device[1]/virtio-backend 1": (
            "  last_avail_idx:       0\r\n    avail:        0x000000003e850400\r\n"
            "    used:         0x000000003e851000\r\n"
        ),
        "xp /1hx 0x3e840402": "000000003e840402: 0x0009\r\n",
        "xp /1hx 0x3e841002": "000000003e841002: 0x0009\r\n",
        "xp /1hx 0x3e84e402": "000000003e84e402: 0x00ae\r\n",
        "xp /1hx 0x3e84f002": "000000003e84f002: 0x00ad\r\n",
        "xp /1hx 0x3e850402": "000000003e850402: 0x0000\r\n",
        "xp /1hx 0x3e851002": "000000003e851002: 0x0000\r\n",
    }

    def fake_monitor(self, line: str, timeout: float = 30) -> str:
        self.assertEqual(timeout, boot.STATE_TIMEOUT)
        return f"\x1b[Ki\x1b[K\x1b[D{line}\x1b[K\r\n{self.REPLIES[line]}(qemu) "

    def test_kept_at_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            # A stand-in for QEMU that prints a banner and then nothing.
            argv = [sys.executable, "-c", "import os, time; os.write(1, b'banner\\r\\n'); time.sleep(60)"]
            g = boot.Guest(argv, Path(t), Path(t), "fake", 1)
            self.addCleanup(g.close)
            g.expect(re.compile("banner"), "banner", 30)
            with mock.patch.object(g, "monitor", side_effect=self.fake_monitor), self.assertRaises(boot.BootError) as err:
                g.expect(re.compile("Shell> "), "Shell prompt")
            self.assertEqual(
                str(err.exception),
                f"fake: no Shell prompt within 1 s; console ends:\nbanner\n\n(QEMU's state when it was given up on: {t}/fake.monitor)",
            )
            gpu = "/machine/peripheral-anon/device[1]/virtio-backend"
            blk = "/machine/peripheral-anon/device[0]/virtio-backend"
            self.assertEqual(
                (Path(t) / "fake.monitor").read_text(),
                "### info status\nVM status: running\n"
                "### info registers -a\nCPU#0\nRIP=000000003eef87ec RFL=00000246\nCPU#1\nRIP=000000003eef8800\n"
                "### info registers -a\nCPU#0\nRIP=000000003eef87ec RFL=00000246\nCPU#1\nRIP=000000003eef8800\n"
                f"### info virtio\n{blk} [virtio-blk]\n{gpu} [virtio-gpu]\n"
                f"### info virtio-queue-status {blk} 0\n"
                "  last_avail_idx:       9\n    avail:        0x000000003e840400\n    used:         0x000000003e841000\n"
                "### xp /1hx 0x3e840402\n000000003e840402: 0x0009\n"
                "### xp /1hx 0x3e841002\n000000003e841002: 0x0009\n"
                f"### info virtio-queue-status {blk} 1\n"
                "  last_avail_idx:       0\n    avail:        0x0000000000000000\n    used:         0x0000000000000000\n"
                f"### info virtio-queue-status {gpu} 0\n"
                "  used_idx:             173\n  last_avail_idx:       173\n"
                "    avail:        0x000000003e84e400\n    used:         0x000000003e84f000\n"
                "### xp /1hx 0x3e84e402\n000000003e84e402: 0x00ae\n"
                "### xp /1hx 0x3e84f002\n000000003e84f002: 0x00ad\n"
                f"### info virtio-queue-status {gpu} 1\n"
                "  last_avail_idx:       0\n    avail:        0x000000003e850400\n    used:         0x000000003e851000\n"
                "### xp /1hx 0x3e850402\n000000003e850402: 0x0000\n"
                "### xp /1hx 0x3e851002\n000000003e851002: 0x0000\n",
            )

    def test_monitor_fails(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            argv = [sys.executable, "-c", "import time; time.sleep(60)"]
            g = boot.Guest(argv, Path(t), Path(t), "fake", 1)
            self.addCleanup(g.close)
            with (
                mock.patch.object(g, "monitor", side_effect=boot.BootError("fake: the monitor failed")),
                self.assertRaises(boot.BootError) as err,
            ):
                g.expect(re.compile("Shell> "), "Shell prompt")
            self.assertEqual(
                str(err.exception),
                f"fake: no Shell prompt within 1 s; console ends:\n\n(QEMU's state when it was given up on: {t}/fake.monitor)",
            )
            self.assertEqual((Path(t) / "fake.monitor").read_text(), "### info status\nfake: the monitor failed\n")

    def test_not_kept(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = boot.Guest([sys.executable, "-c", "import time; time.sleep(60)"], Path(t), Path(t), "fake", 1)
            self.addCleanup(g.close)
            with (
                mock.patch.object(g, "keep_state", side_effect=OSError(28, "No space left on device")),
                self.assertRaises(boot.BootError) as err,
            ):
                g.expect(re.compile("Shell> "), "Shell prompt")
            self.assertEqual(
                str(err.exception),
                "fake: no Shell prompt within 1 s; console ends:\n\n(QEMU's state not kept: [Errno 28] No space left on device)",
            )

    def test_exits_while_kept(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = boot.Guest([sys.executable, "-c", "import time; time.sleep(60)"], Path(t), Path(t), "fake", 1)
            self.addCleanup(g.close)

            def gone(line: str, timeout: float = 30) -> str:
                g.proc.kill()
                g.proc.wait()
                raise boot.BootError(f"fake: monitor `{line}`: gone")

            with mock.patch.object(g, "monitor", side_effect=gone), self.assertRaises(boot.BootError) as err:
                g.expect(re.compile("Shell> "), "Shell prompt")
            self.assertEqual(
                str(err.exception),
                "fake: no Shell prompt within 1 s; console ends:\n\n"
                f"(QEMU's state when it was given up on: {t}/fake.monitor)\nQEMU exited -9: ",
            )

    def test_not_when_qemu_exited(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = boot.Guest([sys.executable, "-c", "pass"], Path(t), Path(t), "fake", 5)
            self.addCleanup(g.close)
            with self.assertRaises(boot.BootError) as err:
                g.expect(re.compile("Shell> "), "Shell prompt")
            self.assertEqual(str(err.exception), "fake: no Shell prompt within 5 s; console ends:\n\nQEMU exited 0: ")
            self.assertFalse((Path(t) / "fake.monitor").exists())


# A stand-in for QEMU and the Shell: a prompt; `fs0:\ticks.nsh` prints a
# tick every 20 ms, 600 of them (or, "fast", 5), then "ticks done" and the
# prompt; 0x03 while it runs stops it at once ("breaks"), restarts the
# firmware into a new Shell ("resets"), or is ignored. Typed keys are
# echoed, as the Shell echoes them; `echo X` says X.
_FAKE_SHELL = r"""
import os, sys, threading, time
mode = sys.argv[1]
lock = threading.Lock()
stop = threading.Event()
def out(s):
    with lock:
        os.write(1, s.encode())
def ticks():
    for n in range(1, (5 if mode == "fast" else 600) + 1):
        if stop.is_set():
            if mode == "breaks":
                out("Shell> ")
            return
        out(f"tick {n}\r\n")
        time.sleep(0.02)
    out("ticks done\r\nShell> ")
out("Shell> ")
line = b""
while (c := os.read(0, 1)):
    if c == b"\x03" and mode == "breaks":
        stop.set()
    elif c == b"\x03" and mode == "resets":
        stop.set()
        time.sleep(0.05)
        out("\r\nUEFI Interactive Shell v2.2\r\nPress ESC in 5 seconds to skip startup.nsh\r\nShell> ")
    elif c == b"\r":
        out("\r\n")
        if line == b"fs0:\\" + b"ticks.nsh":
            stop.clear()
            threading.Thread(target=ticks, daemon=True).start()
        elif line.startswith(b"echo "):
            out(line[5:].decode() + "\r\nShell> ")
        line = b""
    else:
        out(c.decode())
        line += c
"""


class CtrlCTests(unittest.TestCase):
    """A serial Ctrl-C must stop a running Shell script within the bound."""

    def guest(self, t: str, mode: str) -> boot.Guest:
        g = boot.Guest([sys.executable, "-c", _FAKE_SHELL, mode], Path(t), Path(t), "fake", 5)
        self.addCleanup(g.close)
        g.prompt()
        return g

    def test_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            took = boot.ctrl_c_break(self.guest(t, "breaks"), 2)
            self.assertLess(took, 2)

    def test_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = self.guest(t, "ignored")
            with self.assertRaises(boot.BootError) as err:
                boot.ctrl_c_break(g, 1)
            self.assertEqual(
                str(err.exception).split("\n")[0],
                "fake: no Shell prompt after a serial Ctrl-C (0x03) to a running script within 1 s; console ends:",
            )

    def test_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = self.guest(t, "resets")
            with self.assertRaises(boot.BootError) as err:
                boot.ctrl_c_break(g, 5)
            self.assertEqual(
                str(err.exception),
                "fake: the firmware restarted after a serial Ctrl-C (0x03), instead of breaking the script",
            )

    def test_ran_to_its_end(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = self.guest(t, "fast")
            with self.assertRaises(boot.BootError) as err:
                boot.ctrl_c_break(g, 5)
            self.assertEqual(str(err.exception), "fake: a serial Ctrl-C (0x03) did not stop the script: it ran to its end")

    def test_script(self) -> None:
        self.assertEqual(
            boot.TICKS,
            "@echo -off\r\nfor %a run (1 600)\r\n  echo tick %a\r\n  stall 100000\r\nendfor\r\necho ticks done\r\n",
        )


# --- r3 console: the Ctrl-S boot check (patch 0007) -------------------------
# A stand-in for QEMU and the Shell, for Ctrl-S: as _FAKE_SHELL, a prompt and
# `fs0:\ticks.nsh` ticking every 20 ms (600 ticks, or "fast" 5), which 0x03
# breaks. 0x13 while it runs pauses the ticks until the next key ("pauses"),
# pauses them for good ("stuck"), or is ignored ("ignored"). While paused,
# the tick on its way is held, as the Shell holds the OutputString that
# found the pause. DEL (0x7f) at the prompt erases the last character typed,
# or, "ignored", nothing (VT100's Delete at the end of the line).
_FAKE_SHELL_CTRL_S = r"""
import os, sys, threading, time
mode = sys.argv[1]
lock = threading.Lock()
stop = threading.Event()
running = threading.Event()
go = threading.Event()
go.set()
def out(s):
    with lock:
        os.write(1, s.encode())
def ticks():
    for n in range(1, (5 if mode == "fast" else 600) + 1):
        go.wait()
        if stop.is_set():
            out("Shell> ")
            running.clear()
            return
        out(f"tick {n}\r\n")
        time.sleep(0.02)
    out("ticks done\r\nShell> ")
    running.clear()
out("Shell> ")
line = b""
while (c := os.read(0, 1)):
    if running.is_set():
        if c == b"\x03":
            stop.set()
            go.set()
        elif c == b"\x13" and mode in ("pauses", "stuck"):
            go.clear()
        elif mode == "pauses":
            go.set()
        continue
    if c == b"\x7f":
        if mode != "ignored":
            line = line[:-1]
        continue
    if c == b"\r":
        out("\r\n")
        if line == b"fs0:\\" + b"ticks.nsh":
            stop.clear()
            go.set()
            running.set()
            threading.Thread(target=ticks, daemon=True).start()
        elif line.startswith(b"echo "):
            out(line[5:].decode() + "\r\nShell> ")
        line = b""
    else:
        out(c.decode())
        line += c
"""


class CtrlSTests(unittest.TestCase):
    """A serial Ctrl-S must pause a running script's output, and a key resume it."""

    def guest(self, t: str, mode: str) -> boot.Guest:
        g = boot.Guest([sys.executable, "-c", _FAKE_SHELL_CTRL_S, mode], Path(t), Path(t), "fake", 5)
        self.addCleanup(g.close)
        g.prompt()
        return g

    def pause(self, g: boot.Guest) -> dict[str, int]:
        return boot.ctrl_s_pause(g, settle=0.3, quiet=0.5, bound=2)

    def test_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            got = self.pause(self.guest(t, "pauses"))
            self.assertEqual(sorted(got), ["paused_after", "resumed_with"])
            self.assertEqual(got["resumed_with"], got["paused_after"] + 1)
            self.assertGreaterEqual(got["paused_after"], 3)

    def test_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = self.guest(t, "ignored")
            with self.assertRaises(boot.BootError) as err:
                self.pause(g)
            self.assertRegex(
                str(err.exception),
                r"\Afake: a serial Ctrl-S \(0x13\) did not pause the script's output:"
                r" \d+ ticks \(tick \d+ to tick \d+\) in the 0\.5 s from 0\.3 s after it\Z",
            )

    def test_stuck(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = self.guest(t, "stuck")
            with self.assertRaises(boot.BootError) as err:
                self.pause(g)
            self.assertRegex(
                str(err.exception).split("\n")[0],
                r"\Afake: no tick \d+ after a key, to resume output paused by a serial Ctrl-S \(0x13\),"
                r" within 2 s; console ends:\Z",
            )

    def test_del_is_backspace(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            boot.del_backspace(self.guest(t, "pauses"))

    def test_del_is_not_backspace(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = self.guest(t, "ignored")
            with self.assertRaises(boot.BootError) as err:
                boot.del_backspace(g)
            self.assertEqual(
                str(err.exception),
                "fake: a serial DEL (0x7f) is not Backspace: `echo abX`, DEL, `c` printed 'abXc', not 'abc'",
            )

    def test_ran_to_its_end(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            g = self.guest(t, "fast")
            with self.assertRaises(boot.BootError) as err:
                self.pause(g)
            self.assertEqual(
                str(err.exception), "fake: the script ended before a serial Ctrl-S (0x13) could pause it"
            )


# --- end of r3 console: the Ctrl-S boot check --------------------------------


# A stand-in Shell for guard.outcome: on `fs0:\Overrun.efi` it prints the
# application's first line, then, by mode, X64's or AARCH64's exception dump
# for a fault in Overrun.dll (as the guarded images print them, cut), the
# second line and a prompt, or nothing more; "missing" answers as the Shell
# does for a file that is not there.
_FAKE_OVERRUN = r"""
import os, sys
mode = sys.argv[1]
def out(s):
    os.write(1, s.encode())
X64 = (
    "!!!! X64 Exception Type - 0E(#PF - Page-Fault)  CPU Apic ID - 00000000 !!!!\r\n"
    "ExceptionData - 0000000000000002  I:0 R:0 U:0 W:1 P:0 PK:0 SS:0 SGX:0\r\n"
    "RIP  - 000000003AF16E46, CS  - 0000000000000038, RFLAGS - 0000000000010206\r\n"
    "!!!! Find image based on IP(0x3AF16E46) /w/Build/AxlFwGuard/DEBUG_GCC/X64/AxlFwGuardPkg/Overrun/Overrun/"
    "DEBUG/Overrun.dll (ImageBase=000000003AF15000, EntryPoint=000000003AF16DCA) !!!!\r\n"
)
AARCH64 = (
    "\r\n\r\nSynchronous Exception at 0x0000000043DC2F18\r\n"
    "PC 0x000043DC2F18 (0x000043DC1000+0x00001F18) [ 0] Overrun.dll\r\n"
)
out("Shell> ")
line = b""
while (c := os.read(0, 1)):
    if c != b"\r":
        out(c.decode())
        line += c
        continue
    out("\r\n")
    if line == b"fs0:\\Overrun.efi" and mode == "missing":
        out("'fs0:\\Overrun.efi' is not recognized as an internal or external command.\r\nShell> ")
    elif line == b"fs0:\\Overrun.efi":
        out("axlfw-overrun: writing byte 64 of a 64-byte pool buffer\r\n")
        if mode == "x64":
            out(X64)
        elif mode == "aarch64":
            out(AARCH64)
        elif mode == "wrong-image":
            out(X64.replace("Overrun.dll", "DxeCore.dll"))
        elif mode == "silent":
            out("axlfw-overrun: written, no fault\r\nShell> ")
    line = b""
"""


class GuardTests(unittest.TestCase):
    """The guarded image's boot check: Overrun.efi faults, or does not."""

    def guest(self, t: str, mode: str) -> boot.Guest:
        g = boot.Guest([sys.executable, "-c", _FAKE_OVERRUN, mode], Path(t), Path(t), "fake", 2)
        self.addCleanup(g.close)
        g.prompt()
        return g

    def test_outcomes(self) -> None:
        for mode, arch, want in (("x64", "x64", "fault"), ("aarch64", "aarch64", "fault"), ("silent", "x64", "silent"),
                                 ("silent", "aarch64", "silent")):
            with self.subTest(mode=mode, arch=arch), tempfile.TemporaryDirectory() as t:
                self.assertEqual(guard.outcome(self.guest(t, mode), arch, 1), want)

    def test_no_outcome(self) -> None:
        # Each arch's dump is not the other's; a fault placed in another
        # image is not the application's; nothing after the first line.
        for mode, arch in (("x64", "aarch64"), ("aarch64", "x64"), ("wrong-image", "x64"), ("hangs", "x64")):
            with self.subTest(mode=mode, arch=arch), tempfile.TemporaryDirectory() as t:
                with self.assertRaises(boot.BootError) as cm:
                    guard.outcome(self.guest(t, mode), arch, 1)
                self.assertEqual(
                    str(cm.exception).split("\n")[0],
                    "fake: no Overrun.efi's fault or second line within 1 s; console ends:",
                )

    def test_not_started(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(boot.BootError) as cm:
                guard.outcome(self.guest(t, "missing"), "x64", 1)
            self.assertEqual(
                str(cm.exception).split("\n")[0], "fake: no Overrun.efi's first line within 2 s; console ends:"
            )

    def test_expected(self) -> None:
        cases: tuple[tuple[str, str, list[str]], ...] = (("GUARDED", "fault", []), ("GUARDED", "silent", [
            "x64 GUARDED: Overrun.efi did not fault on a write one byte past a pool buffer; fault was expected"]),
            ("DEBUG", "silent", []), ("DEBUG", "fault", [
                "x64 DEBUG: Overrun.efi faulted on a write one byte past a pool buffer; silent was expected"]))
        for flavour, got, problems in cases:
            with self.subTest(flavour=flavour, got=got), tempfile.TemporaryDirectory() as t:
                d = Path(t)
                for name in ("code.fd", "vars.fd", "Shell_X64.efi", "Overrun.efi"):
                    (d / name).write_bytes(b"x")
                s = boot.ImageSet("x64", flavour, d / "code.fd", d / "vars.fd", d / "Shell_X64.efi", "tag")
                fake = mock.MagicMock(spec=boot.Guest)
                with mock.patch.object(boot, "Guest", return_value=fake), \
                        mock.patch.object(guard, "outcome", return_value=got):
                    r = guard.check(s, d / "Overrun.efi", None, d / "logs", 5)
                self.assertEqual((r.facts, r.problems), ({"pool_overrun": got}, problems))
                fake.close.assert_called_once_with()

    def test_app_path(self) -> None:
        self.assertEqual(
            guard.app(Path("/w"), "GCC", "aarch64"), Path("/w/Build/AxlFwGuard/DEBUG_GCC/AARCH64/Overrun.efi")
        )

    def test_boot_checks_need_the_app(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            qemu = mock.patch.object(boot.shutil, "which", return_value="/usr/bin/qemu")
            missing = Path(t) / "Overrun.efi"
            with qemu:
                self.assertEqual(
                    release.boot_checks(
                        Path(t), "tag", None, Path(t) / "logs", {guard.APP: {arch: missing for arch in checks.ARCHES}}
                    ),
                    ({}, [f"{missing}: no Overrun.efi for the guard check"] * 2),
                )


# --- The TCP and virtio-fs boot checks (lib/axlfw/netfs.py) -------------
# A stand-in Shell for reconnect's functions. `load fs0:\TcpHold.efi` listens
# on the port given, accepts one connection and reads 6 bytes, as TcpHold
# says; `dh -p` lists its two sockets while one is held. `reconnect -r`
# drops them and gives the prompt back, or, "hangs", never does; "lingers"
# keeps a socket. fs1: holds race.txt; VfsRace.efi passes, or "vfs-fail"
# fails, or "vfs-hangs" says nothing; "vfs-lost" loses the share after a
# reconnect, "vfs-renumbered" moves it to fs2:; "no-share" has none.
_FAKE_NETFS = r"""
import os, socket, sys, threading
mode, port = sys.argv[1], int(sys.argv[2])
lock = threading.Lock()
held = []
vol = ["Shell"]
lost = [False]
moved = [False]
def out(s):
    with lock:
        os.write(1, s.encode())
def prompt():
    out(vol[0] + "> ")
def serve(srv):
    c, _ = srv.accept()
    held.append(c)
    out("tcphold: accepted\r\n")
    data = b""
    while len(data) < 6:
        data += c.recv(6 - len(data))
    out(f"tcphold: received {len(data)} bytes\r\n")
prompt()
line = b""
while (c := os.read(0, 1)):
    if c != b"\r":
        out(c.decode())
        line += c
        continue
    out("\r\n")
    cmd, line = line.decode(), b""
    if cmd == "load fs0:\\TcpHold.efi":
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        held.clear()
        held.append(srv)
        threading.Thread(target=serve, args=(srv,), daemon=True).start()
        out("tcphold: listening on port 5555\r\n")
    elif cmd.startswith("dh -p "):
        for n, h in enumerate(held):
            out(f"  {0xA1 + n:X}: TCPv4 PciRoot(0x0)/Pci(0x3,0x0)/MAC(525400123456,0x1)/IPv4({'0.0.0.0' if n == 0 else '10.0.2.2'})\r\n")
    elif cmd == "reconnect -r":
        if mode == "hangs":
            continue
        for h in held[1 if mode == "lingers" else 0:]:
            h.close()
        del held[1 if mode == "lingers" else 0:]
        lost[0] = mode == "vfs-lost"
        moved[0] = mode == "vfs-renumbered"
    elif cmd.startswith("ls fs"):
        if cmd == f"ls fs{2 if moved[0] else 1}:\\race.txt" and mode != "no-share" and not lost[0]:
            out("11/26/2026  01:00              3,000  race.txt\r\n")
        else:
            out("ls: File Not Found - 'x'\r\n")
    elif cmd in ("fs0:", "fs1:"):
        vol[0] = "FS" + cmd[2] + ":\\"
    elif cmd == "VfsRace.efi":
        if mode == "vfs-hangs":
            continue
        out("vfsrace: FAIL read 3 differs\r\n" if mode == "vfs-fail" else "vfsrace: PASS cb=1000 fg=5615\r\n")
    prompt()
"""


class NetFsTests(unittest.TestCase):
    """The TCP and virtio-fs boot checks' functions, on a stand-in Shell."""

    def guest(self, t: str, mode: str, port: int = 0) -> boot.Guest:
        g = boot.Guest([sys.executable, "-c", _FAKE_NETFS, mode, str(port)], Path(t), Path(t), "fake", 5)
        self.addCleanup(g.close)
        g.prompt()
        return g

    def test_tcp_returns(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            port = netfs.free_port()
            took, before = netfs.tcp_reconnect(self.guest(t, "returns", port), port, bound=2)
            self.assertEqual(before, [("A1", "0.0.0.0"), ("A2", "10.0.2.2")])
            self.assertLess(took, 2)

    def test_tcp_hangs(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            port = netfs.free_port()
            with self.assertRaises(boot.BootError) as cm:
                netfs.tcp_reconnect(self.guest(t, "hangs", port), port, bound=1)
            self.assertEqual(
                str(cm.exception),
                "fake: reconnect -r did not return within 1 s while TcpHold held an accepted connection"
                " (sockets [('A1', '0.0.0.0'), ('A2', '10.0.2.2')])",
            )

    def test_tcp_socket_lingers(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            port = netfs.free_port()
            with self.assertRaises(boot.BootError) as cm:
                netfs.tcp_reconnect(self.guest(t, "lingers", port), port, bound=2)
            self.assertEqual(
                str(cm.exception), "fake: TcpHold's sockets after reconnect -r are [('A1', '0.0.0.0')], not none"
            )

    def test_sockets_parsed(self) -> None:
        dh = (
            "Handle dump by protocol 'TCPv4'\r\n"
            "  A1: TCPv4 PciRoot(0x0)/Pci(0x3,0x0)/MAC(525400123456,0x1)/IPv4(0.0.0.0)\r\n"
            "  B7: TCPv4 \r\n"
            "  C2: TCPv4 PciRoot(0x0)/Pci(0x3,0x0)/MAC(525400123456,0x1)/IPv4(10.0.2.2)\r\n"
        )
        self.assertEqual(netfs.tcphold_sockets(dh), [("A1", "0.0.0.0"), ("C2", "10.0.2.2")])

    def test_vfs_passes(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            line, took = netfs.vfs_race(self.guest(t, "vfs"), bound=2)
            self.assertEqual(line, "vfsrace: PASS cb=1000 fg=5615")
            self.assertLess(took, 2)

    def test_vfs_fails(self) -> None:
        for mode, want in (
            ("vfs-fail", "fake: vfsrace: FAIL read 3 differs"),
            ("no-share", "fake: no virtio-fs volume holds race.txt"),
            ("vfs-lost", "fake: after reconnect -r, no virtio-fs volume holds race.txt"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as t:
                with self.assertRaises(boot.BootError) as cm:
                    netfs.vfs_race(self.guest(t, mode), bound=2)
                self.assertEqual(str(cm.exception), want)

    def test_vfs_share_renumbered(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            line, _ = netfs.vfs_race(self.guest(t, "vfs-renumbered"), bound=2)
            self.assertEqual(line, "vfsrace: PASS cb=1000 fg=5615")

    def test_vfs_hangs(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(boot.BootError) as cm:
                netfs.vfs_race(self.guest(t, "vfs-hangs"), bound=1)
            self.assertEqual(str(cm.exception).split("\n")[0], "fake: no VfsRace.efi's result within 1 s; console ends:")

    def test_app_paths(self) -> None:
        self.assertEqual(
            (netfs.tcp_app(Path("/w"), "GCC", "x64"), netfs.vfs_app(Path("/w"), "GCC", "aarch64")),
            (Path("/w/Build/Repro/DEBUG_GCC/X64/TcpHold.efi"), Path("/w/Build/Repro/DEBUG_GCC/AARCH64/VfsRace.efi")),
        )

    def test_virtiofsd_found(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            fsd = Path(t) / "virtiofsd"
            fsd.write_text("#!/bin/sh\n")
            fsd.chmod(0o755)
            with mock.patch.dict(os.environ, {"AXLFW_VIRTIOFSD": str(fsd)}):
                self.assertEqual(netfs.virtiofsd(), fsd)
            with mock.patch.dict(os.environ, {"AXLFW_VIRTIOFSD": ""}), mock.patch.object(
                netfs.shutil, "which", return_value=str(fsd)
            ):
                self.assertEqual(netfs.virtiofsd(), fsd)
            with mock.patch.dict(os.environ, {"AXLFW_VIRTIOFSD": ""}), mock.patch.object(
                netfs.shutil, "which", return_value=None
            ), mock.patch.object(netfs.os, "access", return_value=False):
                self.assertIsNone(netfs.virtiofsd())
            fsd.chmod(0o644)
            self.assertEqual(netfs.virtiofsd_problems(fsd), [f"{fsd}: virtiofsd is not an executable file"])

    def boot_checks(self, t: Path, env: dict[str, str], fsd: Path | None) -> tuple[dict[str, dict[str, object]], list[str], str]:
        apps = {name: {arch: t / name for arch in checks.ARCHES} for name in (netfs.TCP_APP, netfs.VFS_APP)}
        for per in apps.values():
            for p in per.values():
                p.write_bytes(b"x")
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(boot.shutil, "which", return_value="/usr/bin/x"),
            mock.patch.object(netfs, "virtiofsd", return_value=fsd),
            mock.patch.object(boot, "run", side_effect=lambda *_a: boot.Result()),  # pyright: ignore[reportUnknownLambdaType]
            mock.patch.object(tpm, "check", side_effect=lambda *_a: boot.Result()),  # pyright: ignore[reportUnknownLambdaType]
            mock.patch.object(netfs, "check_tcp", return_value=boot.Result(facts={"tcp_reconnect": True})) as tcp,
            mock.patch.object(netfs, "check_vfs", return_value=boot.Result(facts={"vfs_race": "pass"})) as vfs,
            contextlib.redirect_stderr(err),
        ):
            facts, problems = release.boot_checks(t, "tag", None, t / "logs", apps)
        self.assertEqual(
            sorted((c.args[0].arch, c.args[0].flavour) for c in tcp.call_args_list),
            [("aarch64", "DEBUG"), ("aarch64", "RELEASE"), ("x64", "DEBUG"), ("x64", "RELEASE")],
        )
        self.assertEqual(vfs.call_count, 0 if fsd is None else 4)
        return facts, problems, err.getvalue()

    def test_boot_checks_run_on_debug_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            facts, problems, err = self.boot_checks(Path(t), {"AXLFW_REQUIRE_VIRTIOFS": ""}, Path(sys.executable))
            self.assertEqual((problems, err), ([], ""))
            self.assertEqual(
                {d: f for d, f in facts.items() if f},
                {d: {"tcp_reconnect": True, "vfs_race": "pass"} for d in ("x64", "aarch64", "release/x64", "release/aarch64")},
            )

    def test_boot_checks_skip_without_virtiofsd(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            facts, problems, err = self.boot_checks(Path(t), {"AXLFW_REQUIRE_VIRTIOFS": ""}, None)
            self.assertEqual(problems, [])
            self.assertEqual(
                err,
                "SKIP: the virtio-fs boot check (patch 0011): no virtiofsd (AXLFW_VIRTIOFSD, PATH or"
                " /usr/libexec/virtiofsd); AXLFW_REQUIRE_VIRTIOFS=1 makes this a failure\n",
            )
            self.assertEqual(facts["release/aarch64"], {"tcp_reconnect": True, "vfs_race": "skipped"})
            self.assertEqual(facts["guarded/x64"], {})

    def test_boot_checks_require_virtiofsd(self) -> None:
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"AXLFW_REQUIRE_VIRTIOFS": "1"}), \
                mock.patch.object(boot.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(netfs, "virtiofsd", return_value=None):
            apps = {netfs.VFS_APP: {arch: Path(t) / "VfsRace.efi" for arch in checks.ARCHES}}
            apps[netfs.VFS_APP]["x64"].write_bytes(b"x")
            self.assertEqual(
                release.boot_checks(Path(t), "tag", None, Path(t) / "logs", apps),
                (
                    {},
                    [
                        "virtiofsd not found (AXLFW_VIRTIOFSD, PATH or /usr/libexec/virtiofsd):"
                        " AXLFW_REQUIRE_VIRTIOFS=1 requires the virtio-fs boot check"
                    ],
                ),
            )


    def test_require_without_the_app(self) -> None:
        # `build.sh check` has no VfsRace.efi: required, that is a failure.
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"AXLFW_REQUIRE_VIRTIOFS": "1"}), \
                mock.patch.object(boot.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual(
                release.boot_checks(Path(t), "tag", None, Path(t) / "logs"),
                (
                    {},
                    [
                        "AXLFW_REQUIRE_VIRTIOFS=1 requires the virtio-fs boot check, which needs VfsRace.efi"
                        " (a build has it; `build.sh check` does not)"
                    ],
                ),
            )

    def test_app_for_every_arch(self) -> None:
        with tempfile.TemporaryDirectory() as t, mock.patch.object(boot.shutil, "which", return_value="/usr/bin/x"):
            app = Path(t) / "TcpHold.efi"
            app.write_bytes(b"x")
            self.assertEqual(
                release.boot_checks(Path(t), "tag", None, Path(t) / "logs", {netfs.TCP_APP: {"x64": app}}),
                ({}, ["no TcpHold.efi for aarch64, for the TCP reconnect check"]),
            )


# --- end of the TCP and virtio-fs boot checks --------------------------------


# A Shell with one NIC for the reconnect check, run as `python -c <this>
# <mode> <debug log>`. `ifconfig -l` lists eth0 (with no address until a
# DHCP lease), `ifconfig -s eth0 dhcp` leases 10.0.2.15 after two more
# `ifconfig -l eth0`, and `reconnect -r` behaves as mode says: "works"
# writes DxeNetLib's "Failed to locate EFI_RNG_PROTOCOL" to the debug log and
# brings the prompt back, as a DEBUG image with patch 0015 does; "has-rng"
# brings it back writing nothing, as an image that never lacked an RNG;
# "asserts" writes edk2's ASSERT lines too and hangs, as a DEBUG image's
# CpuDeadLoop does; "asserts-returns" writes them and still brings the prompt
# back; "no-eth0" leaves no eth0 listed; "no-lease" never leases; "no-nic"
# never lists eth0; "hangs" never brings the prompt back, writing no ASSERT;
# "boot-window" writes the RNG error at startup, before
# the prompt, and nothing in `reconnect -r`.
_FAKE_NET_SHELL = r"""
import os, sys
mode, debug = sys.argv[1], sys.argv[2]
state = {"eth0": mode != "no-nic", "dhcp": False, "asks": 0}
if mode == "boot-window":
    with open(debug, "a") as f:
        f.write("Failed to locate EFI_RNG_PROTOCOL: Not Found\r\n")
def out(s):
    os.write(1, s.encode())
def ifconfig():
    if not state["eth0"]:
        return ""
    addr = "0.0.0.0"
    if state["dhcp"] and mode != "no-lease":
        state["asks"] += 1
        if state["asks"] > 2:
            addr = "10.0.2.15"
    return f"---\r\n\r\nname         : eth0\r\nMedia State  : Media present\r\n\r\nipv4 address : {addr}\r\n\r\n---\r\n"
out("Shell> ")
line = b""
while (c := os.read(0, 1)):
    if c != b"\r":
        out(c.decode())
        line += c
        continue
    out("\r\n")
    if line in (b"ifconfig -l", b"ifconfig -l eth0"):
        out(ifconfig())
    elif line == b"ifconfig -s eth0 dhcp":
        state["dhcp"] = True
    elif line == b"reconnect -r":
        if mode not in ("has-rng", "boot-window"):
            with open(debug, "a") as f:
                f.write("Failed to locate EFI_RNG_PROTOCOL: Not Found\r\n")
        if mode.startswith("asserts"):
            with open(debug, "a") as f:
                f.write("ASSERT_EFI_ERROR (Status = Not Found)\r\n")
                f.write("ASSERT [Ip4Dxe] DxeNetLib.c(944): !(((RETURN_STATUS)(Status)) >= 0x8000000000000000ULL)\r\n")
            if mode == "asserts":
                line = b""
                continue
        if mode == "hangs":
            line = b""
            continue
        if mode == "no-eth0":
            state["eth0"] = False
    out("Shell> ")
    line = b""
"""


class ReconnectTests(unittest.TestCase):
    """`reconnect -r` must come back without an ASSERT, and the network with it."""

    def guest(self, t: str, mode: str) -> tuple[boot.Guest, Path]:
        debug = Path(t) / "debug.log"
        debug.write_text("")
        g = boot.Guest([sys.executable, "-c", _FAKE_NET_SHELL, mode, str(debug)], Path(t), Path(t), "fake", 5)
        self.addCleanup(g.close)
        g.prompt()
        return g, debug

    def failure(self, mode: str, bound: float = 5, debug_flavour: bool = True) -> str:
        with tempfile.TemporaryDirectory() as t:
            g, debug = self.guest(t, mode)
            with self.assertRaises(boot.BootError) as err:
                boot.reconnect_network(g, debug, debug_flavour, bound, 2)
            return str(err.exception)

    def test_works(self) -> None:
        for mode, debug_flavour in (("works", True), ("works", False), ("has-rng", False)):
            with self.subTest(mode=mode, debug_flavour=debug_flavour), tempfile.TemporaryDirectory() as t:
                g, debug = self.guest(t, mode)
                took = boot.reconnect_network(g, debug, debug_flavour, 5, 2)
                self.assertLess(took, 5)

    def test_has_rng(self) -> None:
        self.assertEqual(
            self.failure("has-rng"),
            "fake: no 'Failed to locate EFI_RNG_PROTOCOL' on the debug channel in `reconnect -r`: the network "
            "stack never started without an RNG, so it tested nothing",
        )

    def test_hangs(self) -> None:
        self.assertEqual(
            self.failure("hangs", 1).split("\n")[0],
            "fake: no prompt after `reconnect -r` within 1 s; console ends:",
        )

    def test_boot_window(self) -> None:
        self.assertEqual(self.failure("boot-window"), self.failure("has-rng"))

    def test_no_lease_release(self) -> None:
        self.assertEqual(
            self.failure("no-lease", debug_flavour=False),
            "fake: eth0 has no DHCP lease (10.0.2.15) within 2 s after `reconnect -r`: its ipv4 address is 0.0.0.0",
        )

    def test_asserts(self) -> None:
        self.assertEqual(
            self.failure("asserts", 1),
            "fake: the firmware asserted in `reconnect -r`: "
            "ASSERT [Ip4Dxe] DxeNetLib.c(944): !(((RETURN_STATUS)(Status)) >= 0x8000000000000000ULL)",
        )

    def test_asserts_returns(self) -> None:
        self.assertEqual(
            self.failure("asserts-returns"),
            "fake: the firmware asserted in `reconnect -r`: "
            "ASSERT [Ip4Dxe] DxeNetLib.c(944): !(((RETURN_STATUS)(Status)) >= 0x8000000000000000ULL)",
        )

    def test_no_eth0_after(self) -> None:
        self.assertEqual(self.failure("no-eth0"), "fake: `ifconfig -l` lists no eth0 after `reconnect -r`")

    def test_no_lease(self) -> None:
        self.assertEqual(
            self.failure("no-lease"),
            "fake: eth0 has no DHCP lease (10.0.2.15) within 2 s after `reconnect -r`: its ipv4 address is 0.0.0.0",
        )

    def test_no_nic(self) -> None:
        self.assertEqual(self.failure("no-nic"), "fake: `ifconfig -l` lists no eth0 before `reconnect -r`")

    def test_assert_line(self) -> None:
        self.assertEqual(
            boot.first_assert(
                "Failed to locate EFI_RNG_PROTOCOL: Not Found\nASSERT_EFI_ERROR (Status = Not Found)\n"
                "ASSERT DxeNetLib.c(944): !(((RETURN_STATUS)(Status)) >= 0x8000000000000000ULL)\n"
            ),
            "ASSERT DxeNetLib.c(944): !(((RETURN_STATUS)(Status)) >= 0x8000000000000000ULL)",
        )
        self.assertIsNone(boot.first_assert("Tcg2: Not Found\nASSERT_EFI_ERROR is a word here\n"))

    def test_argv(self) -> None:
        x64 = ["qemu", "-machine", "q35", "-accel", "kvm", "-cpu", "host", "-m", "1024"]
        self.assertEqual(
            boot.reconnect_argv(x64, "x64", Path("/d.log")),
            [
                "qemu", "-machine", "q35", "-accel", "kvm", "-cpu", "host,-rdrand,-rdseed", "-m", "1024",
                "-debugcon", "file:/d.log", "-global", "isa-debugcon.iobase=0x402",
                "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0,romfile=", "-device", "virtio-rng-pci",
            ],
        )  # fmt: skip
        aa64 = ["qemu", "-machine", "virt", "-accel", "tcg", "-cpu", "max"]
        self.assertEqual(
            boot.reconnect_argv(aa64, "aarch64", Path("/d.log")),
            [
                "qemu", "-machine", "virt", "-accel", "tcg", "-cpu", "cortex-a57",
                "-serial", "file:/d.log",
                "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0,romfile=", "-device", "virtio-rng-pci",
            ],
        )  # fmt: skip


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
            with mock.patch.object(boot.shutil, "which", return_value="/usr/bin/swtpm"):
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
                        "swtpm not found on PATH: the TPM boot check needs it",
                    ],
                )

    def test_no_swtpm(self) -> None:
        def which(name: str) -> str | None:
            return None if name == "swtpm" else f"/usr/bin/{name}"

        with tempfile.TemporaryDirectory() as t, mock.patch.object(boot.shutil, "which", side_effect=which):
            self.assertEqual(
                release.boot_checks(Path(t), "tag", None, Path(t) / "logs"),
                ({}, ["swtpm not found on PATH: the TPM boot check needs it"]),
            )

    def test_no_pcr_app(self) -> None:
        with tempfile.TemporaryDirectory() as t, mock.patch.object(boot.shutil, "which", return_value="/usr/bin/x"):
            apps = {tpm.APP: {arch: tpm.app(Path(t), "GCC", arch) for arch in checks.ARCHES}}
            self.assertEqual(
                release.boot_checks(Path(t), "tag", None, Path(t) / "logs", apps),
                (
                    {},
                    [
                        f"{t}/Build/AxlFwTpm/DEBUG_GCC/X64/PcrRead.efi: no PcrRead.efi for the TPM check",
                        f"{t}/Build/AxlFwTpm/DEBUG_GCC/AARCH64/PcrRead.efi: no PcrRead.efi for the TPM check",
                    ],
                ),
            )


    def test_guest_apps(self) -> None:
        # build.sh's guest_apps loop builds each of these, for each arch.
        w = Path("/w/Build")
        self.assertEqual(
            release.guest_apps(Path("/w"), "GCC"),
            {
                "Overrun.efi": {
                    "x64": w / "AxlFwGuard/DEBUG_GCC/X64/Overrun.efi",
                    "aarch64": w / "AxlFwGuard/DEBUG_GCC/AARCH64/Overrun.efi",
                },
                "PcrRead.efi": {
                    "x64": w / "AxlFwTpm/DEBUG_GCC/X64/PcrRead.efi",
                    "aarch64": w / "AxlFwTpm/DEBUG_GCC/AARCH64/PcrRead.efi",
                },
                "TcpHold.efi": {
                    "x64": w / "Repro/DEBUG_GCC/X64/TcpHold.efi",
                    "aarch64": w / "Repro/DEBUG_GCC/AARCH64/TcpHold.efi",
                },
                "VfsRace.efi": {
                    "x64": w / "Repro/DEBUG_GCC/X64/VfsRace.efi",
                    "aarch64": w / "Repro/DEBUG_GCC/AARCH64/VfsRace.efi",
                },
            },
        )


_SEPARATOR = hashlib.sha256(bytes(32) + hashlib.sha256(bytes(4)).digest()).hexdigest()
_PCR0 = "2acee488578adc6a12e187dfb51f541eb7eea2699084716bb498bc273ce5404a"


def _pcr_output(pcrs: dict[int, str] | None = None, done: bool = True) -> str:
    """PcrRead.efi's output, as the Shell prints it (CRLF, PrintLib's upper-case hex), for pcrs."""
    values = pcrs if pcrs is not None else {n: (_PCR0 if n == 0 else _SEPARATOR) for n in range(8)}
    lines = [f"axlfw-pcr: sha256 PCR{n} {v.upper()}" for n, v in values.items()]
    return "".join(f"{x}\r\n" for x in [*lines, *(["axlfw-pcr: done"] if done else [])])


# A stand-in for QEMU with a TPM: it connects to the TPM's chardev socket
# (as QEMU connects to swtpm's control socket), writes its argv to
# $AXLFW_FAKE_ARGV, and runs a stand-in Shell whose PcrRead.efi prints
# $AXLFW_FAKE_PCRS; in mode "fault" it stops after the banner.
_FAKE_TPM_QEMU = r"""
import json, os, socket, sys, time
argv = sys.argv[1:]
open(os.environ["AXLFW_FAKE_ARGV"], "w").write(json.dumps(argv))
chardev = argv[argv.index("-chardev", argv.index("-chardev") + 1) + 1]
path = dict(kv.split("=", 1) for kv in chardev.split(",")[1:])["path"]
tpm = socket.socket(socket.AF_UNIX)
tpm.connect(path)
if os.environ["AXLFW_FAKE_MODE"] == "fault":
    os.write(1, b"UEFI firmware (version x)\r\nSynchronous Exception at 0x49748\r\n")
    time.sleep(60)
os.write(1, b"Shell> ")
line = b""
while (c := os.read(0, 1)):
    if c == b"\r":
        os.write(1, b"\r\n")
        if line == b"fs0:\\PcrRead.efi":
            os.write(1, os.environ["AXLFW_FAKE_PCRS"].encode())
        os.write(1, b"Shell> ")
        line = b""
    else:
        os.write(1, c)
        line += c
"""
# A stand-in for swtpm: its control socket, until the one client it
# accepts goes.
_FAKE_SWTPM = r"""
import socket, sys
ctrl = sys.argv[sys.argv.index("--ctrl") + 1]
s = socket.socket(socket.AF_UNIX)
s.bind(ctrl.split("path=", 1)[1])
s.listen(1)
s.settimeout(30)
c, _ = s.accept()
c.settimeout(60)
while c.recv(64):
    pass
"""


class PcrTests(unittest.TestCase):
    """PcrRead.efi's output: eight SHA-256 PCRs, the firmware measured, the separators."""

    def test_good(self) -> None:
        pcrs, problems = tpm.pcr_problems(_pcr_output())
        self.assertEqual(problems, [])
        self.assertEqual(pcrs, {n: (_PCR0 if n == 0 else _SEPARATOR) for n in range(8)})

    def test_separator(self) -> None:
        self.assertEqual(tpm.SEPARATOR_ONLY, "3d458cfe55cc03ea1f443f1562beec8df51c75e14a9fcf9a7234a13f198e7969")

    def test_refused(self) -> None:
        good = {n: (_PCR0 if n == 0 else _SEPARATOR) for n in range(8)}
        other = "11" * 32
        for name, out, want in (
            ("nothing", "", [
                "PCRs printed: [], not 0-7 in order",
                "no 'axlfw-pcr: done' line",
                f"PCR3 is None, not the separator alone ({_SEPARATOR})",
                f"PCR6 is None, not the separator alone ({_SEPARATOR})",
            ]),
            ("an error", "axlfw-pcr: error: no Tcg2 protocol: Not Found\r\n", [
                "axlfw-pcr: error: no Tcg2 protocol: Not Found",
                "PCRs printed: [], not 0-7 in order",
                "no 'axlfw-pcr: done' line",
                f"PCR3 is None, not the separator alone ({_SEPARATOR})",
                f"PCR6 is None, not the separator alone ({_SEPARATOR})",
            ]),
            ("no done", _pcr_output(done=False), ["no 'axlfw-pcr: done' line"]),
            ("PCR7 missing", _pcr_output({n: v for n, v in good.items() if n != 7}),
             ["PCRs printed: [0, 1, 2, 3, 4, 5, 6], not 0-7 in order"]),
            ("out of order", _pcr_output({1: good[1], 0: good[0], **{n: good[n] for n in range(2, 8)}}),
             ["PCRs printed: [1, 0, 2, 3, 4, 5, 6, 7], not 0-7 in order"]),
            ("twice", _pcr_output() + "axlfw-pcr: sha256 PCR7 " + "AB" * 32 + "\r\n",
             ["PCRs printed: [0, 1, 2, 3, 4, 5, 6, 7, 7], not 0-7 in order"]),
            ("short", _pcr_output({**good, 5: "ab"}), ["PCR5 is ab, not 64 hex digits"]),
            ("PCR0 zero", _pcr_output({**good, 0: "0" * 64}),
             [f"PCR0 is {'0' * 64}: the firmware was not measured"]),
            ("PCR0 separator", _pcr_output({**good, 0: _SEPARATOR}),
             [f"PCR0 is {_SEPARATOR}: the firmware was not measured"]),
            ("PCR3 other", _pcr_output({**good, 3: other}),
             [f"PCR3 is {other}, not the separator alone ({_SEPARATOR})"]),
            ("PCR6 zero", _pcr_output({**good, 6: "0" * 64}),
             [f"PCR6 is {'0' * 64}, not the separator alone ({_SEPARATOR})"]),
        ):
            with self.subTest(name):
                self.assertEqual(tpm.pcr_problems(out)[1], want)

    def test_devices(self) -> None:
        self.assertEqual(tpm.DEVICE, {"x64": "tpm-tis", "aarch64": "tpm-tis-device"})

    def test_app(self) -> None:
        self.assertEqual(
            tpm.app(Path("/w"), "GCC", "aarch64"), Path("/w/Build/AxlFwTpm/DEBUG_GCC/AARCH64/PcrRead.efi")
        )


class TpmCheckTests(unittest.TestCase):
    """tpm.check against stand-ins for QEMU and swtpm."""

    def run_check(self, t: Path, mode: str, pcrs: str, with_app: bool = True) -> tuple[boot.Result, list[str]]:
        bin_dir = t / "bin"
        bin_dir.mkdir()
        for name, script in (("qemu-system-aarch64", _FAKE_TPM_QEMU), ("swtpm", _FAKE_SWTPM)):
            (bin_dir / name).write_text(f"#!{sys.executable}\n{script}")
            (bin_dir / name).chmod(0o755)
        for name in ("code.fd", "vars.fd", "Shell_AARCH64.efi", "PcrRead.efi"):
            (t / name).write_bytes(b"x")
        s = boot.ImageSet("aarch64", "DEBUG", t / "code.fd", t / "vars.fd", t / "Shell_AARCH64.efi", "tag")
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "AXLFW_FAKE_ARGV": str(t / "argv.json"),
            "AXLFW_FAKE_MODE": mode,
            "AXLFW_FAKE_PCRS": pcrs,
        }
        with mock.patch.dict(os.environ, env):
            r = tpm.check(s, t / "PcrRead.efi" if with_app else None, bin_dir, t / "logs", 5)
        return r, json.loads((t / "argv.json").read_text())

    def test_pcrs_read(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            r, argv = self.run_check(Path(t), "shell", _pcr_output())
            self.assertEqual(r.problems, [])
            self.assertEqual(r.facts, {"tpm": {"device": "tpm-tis-device", "shell": True, "pcrs_read": True}})
            self.assertEqual(argv[argv.index("-tpmdev") + 1], "emulator,id=tpm0,chardev=chrtpm")
            self.assertEqual(argv[argv.index("-tpmdev") + 3], "tpm-tis-device,tpmdev=tpm0")

    def test_shell_only(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            r, _ = self.run_check(Path(t), "shell", "", with_app=False)
            self.assertEqual(r.problems, [])
            self.assertEqual(r.facts, {"tpm": {"device": "tpm-tis-device", "shell": True, "pcrs_read": False}})

    def test_bad_pcrs(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            r, _ = self.run_check(Path(t), "shell", _pcr_output(done=False))
            self.assertEqual(r.problems, ["aarch64 DEBUG: with a TPM, PcrRead.efi: no 'axlfw-pcr: done' line"])
            self.assertEqual(r.facts, {"tpm": {"device": "tpm-tis-device", "shell": True, "pcrs_read": False}})

    def test_fault(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            r, _ = self.run_check(Path(t), "fault", "")
            self.assertEqual(len(r.problems), 1)
            self.assertEqual(
                r.problems[0].split("\n")[:3],
                [
                    "aarch64 DEBUG: with a TPM (tpm-tis-device): aarch64-debug-tpm: no Shell prompt within 5 s;"
                    " console ends:",
                    "UEFI firmware (version x)",
                    "Synchronous Exception at 0x49748",
                ],
            )
            self.assertEqual(r.facts, {"tpm": {"device": "tpm-tis-device", "shell": False, "pcrs_read": False}})

    def test_no_swtpm(self) -> None:
        with mock.patch.object(boot.shutil, "which", return_value=None):
            self.assertEqual(tpm.missing_swtpm(), ["swtpm not found on PATH: the TPM boot check needs it"])


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


FOO = "PartPkg/Library/FooLib/foo"
BAR = "PartPkg/Library/BarLib/bar"
FDT = "MdePkg/Library/BaseFdtLib/libfdt"
UNUSED = "PartPkg/Library/UnusedLib/unused"
# Each fixture part's licence files, as release.licenses copies them.
FIXTURE_TOP = [
    "LICENSES/edk2-License.txt",
    "LICENSES/MdePkg_Library_BaseFdtLib_libfdt/BSD-2-Clause",
    "LICENSES/MdePkg_Library_BaseFdtLib_libfdt/GPL",
    "LICENSES/PartPkg_Library_BarLib_bar/COPYING",
    "LICENSES/PartPkg_Library_FooLib_foo/LICENSE",
    "LICENSES/PartPkg_Library_IaLib_ia/LICENSE",
    "LICENSES/PartPkg_Library_UnusedLib_unused/LICENSE.txt",
]
FIXTURE_LIST: list[dict[str, object]] = [
    {"path": "LICENSES/edk2-License.txt", "chosen": True},
    {"path": "LICENSES/MdePkg_Library_BaseFdtLib_libfdt/BSD-2-Clause", "chosen": True},
    {"path": "LICENSES/MdePkg_Library_BaseFdtLib_libfdt/GPL", "chosen": False},
    {"path": "LICENSES/PartPkg_Library_BarLib_bar/COPYING", "chosen": True},
    {"path": "LICENSES/PartPkg_Library_FooLib_foo/LICENSE", "chosen": True},
]


class LicenceTests(unittest.TestCase):
    """The per-image licence lists: the report's derivation and the check."""

    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        shutil.copytree(FIXTURES / "licenses", self.root / "fx")
        self.src = self.root / "fx" / "src"
        self.overlay = self.root / "fx" / "overlay"
        (self.src / "gitmodules").rename(self.src / ".gitmodules")
        self.roots = [self.overlay, self.src]
        self.subs = release.submodule_paths(self.src)
        text = (FIXTURES / "licenses" / "report.txt").read_text()
        text = text.replace("@SRC@", str(self.src)).replace("@OVERLAY@", str(self.overlay))
        self.report_text = text.replace("@WS@", str(self.root / "ws"))
        self.stage = self.root / "stage"
        for path in FIXTURE_TOP:
            (self.stage / path).parent.mkdir(parents=True, exist_ok=True)
            (self.stage / path).write_text(path)

    def tearDown(self) -> None:
        self._t.cleanup()

    def test_submodules(self) -> None:
        self.assertEqual(self.subs, [FDT, BAR, FOO, "PartPkg/Library/IaLib/ia", UNUSED])

    def test_parse_report(self) -> None:
        r = licenses.parse_report(self.report_text)
        self.assertEqual(r.content, "LIBRARY, FLASH")
        self.assertEqual(
            {fd: [(v.name, v.entries) for v in vols] for fd, vols in r.fds.items()},
            {
                "FIXTURE_CODE": [
                    ("FVMAIN_COMPACT", ["PEI Apriori", f"Pei ({self.src}/AppPkg/Pei/Pei.inf)", "DXEFV"]),
                    (
                        "DXEFV",
                        ["DXE Apriori", f"Dxe ({self.src}/AppPkg/Dxe/Dxe.inf)", f"{self.src}/AppPkg/Logo.bmp"],
                    ),
                ],
                "FIXTURE_OTHER": [("OTHERFV", [f"Other ({self.src}/AppPkg/Other/Other.inf)"])],
            },
        )
        self.assertEqual(
            r.modules,
            {
                "AppPkg/Pei/Pei.inf": [f"{self.src}/MdePkg/Library/BaseFdtLib/BaseFdtLib.inf"],
                "AppPkg/Dxe/Dxe.inf": [
                    f"{self.src}/PartPkg/Library/FooLib/FooLib.inf",
                    f"{self.overlay}/PartPkg/Library/BarLib/BarLib.inf",
                    f"{self.src}/PartPkg/Library/IaLib/IaLib.inf",
                ],
                "AppPkg/Other/Other.inf": [f"{self.src}/PartPkg/Library/UnusedLib/UnusedLib.inf"],
            },
        )

    def test_not_a_report(self) -> None:
        cases = {
            "nothing": ("", "no 'Report Content:' line: not an edk2 build report"),
            "no flash": (
                self.report_text.replace("LIBRARY, FLASH", "LIBRARY"),
                "the report holds LIBRARY, not LIBRARY and FLASH",
            ),
            "no FD": (
                self.report_text.replace("FD Name:", "FD Gone:"),
                "no firmware device: the report has no FLASH section",
            ),
        }
        for label, (text, want) in cases.items():
            with self.subTest(label), self.assertRaises(licenses.ReportError) as cm:
                licenses.parse_report(text)
            self.assertEqual(str(cm.exception), want)

    def test_inf_sources(self) -> None:
        foo = self.src / "PartPkg/Library/FooLib/FooLib.inf"
        self.assertEqual(licenses.inf_sources(foo, "X64"), ["FooLib.c", "foo/src/a.c"])
        ia = self.src / "PartPkg/Library/IaLib/IaLib.inf"
        self.assertEqual(licenses.inf_sources(ia, "X64"), ["IaLib.c"])
        self.assertEqual(licenses.inf_sources(ia, "ARM"), ["IaLib.c", "ia/x.c"])
        foo.write_text(foo.read_text().replace("DEFINE FOO_TOP", "DEFINE FOO_GONE"))
        with self.assertRaises(ValueError) as cm:
            licenses.inf_sources(foo, "X64")
        self.assertEqual(str(cm.exception), f"{foo}: [Sources] entry '$(FOO_PATH)/a.c': no DEFINE for $(FOO_TOP)")

    def test_image_parts(self) -> None:
        r = licenses.parse_report(self.report_text)
        # The nested DXEFV's Dxe counts; FIXTURE_OTHER's Other does not; the
        # overlay's BarLib, first in PACKAGES_PATH, compiles bar; IaLib's
        # part is IA32's and ARM's only, and its X64 entry MSFT's.
        self.assertEqual(licenses.image_parts(r, "FIXTURE_CODE", self.roots, self.subs, "X64"), ({FOO, BAR, FDT}, []))
        self.assertEqual(licenses.image_parts(r, "FIXTURE_OTHER", self.roots, self.subs, "X64"), ({UNUSED}, []))
        self.assertEqual(
            licenses.image_parts(r, "NONE", self.roots, self.subs, "X64"),
            (set[str](), ["the build report has no FD NONE, or no volume in it"]),
        )

    def test_image_parts_problems(self) -> None:
        text = self.report_text.replace("0x00001000 ", "0x00001000 4A1D2C3B-0000-0000-0000-000000000000\n0x00001100 ")
        text = text.replace("Module INF Path:      AppPkg/Pei/Pei.inf", "Module INF Path:      AppPkg/Pei/Gone.inf")
        r = licenses.parse_report(text)
        self.assertEqual(
            licenses.image_parts(r, "FIXTURE_CODE", self.roots, self.subs, "X64"),
            (
                {FOO, BAR},
                [
                    f"FD FIXTURE_CODE, volume FVMAIN_COMPACT: Pei ({self.src}/AppPkg/Pei/Pei.inf) has no module section",
                    "FD FIXTURE_CODE, volume DXEFV: cannot tell what '4A1D2C3B-0000-0000-0000-000000000000' is",
                ],
            ),
        )

    def test_image_licenses(self) -> None:
        self.assertEqual(licenses.image_licenses({FOO, BAR, FDT}, FIXTURE_TOP), FIXTURE_LIST)
        self.assertEqual(licenses.image_licenses(set(), FIXTURE_TOP), FIXTURE_LIST[:1])

    def _check(self, listed: object, parts: set[str] | None = None) -> list[str]:
        return licenses.check_image_licenses("x64", listed, FIXTURE_TOP, self.stage, parts)

    def test_check_passes(self) -> None:
        self.assertEqual(self._check(FIXTURE_LIST, {FOO, BAR, FDT}), [])
        self.assertEqual(self._check(FIXTURE_LIST), [])

    def test_check_refuses(self) -> None:
        edk2, bsd, gpl, bar, foo = FIXTURE_LIST
        stray = "LICENSES/PartPkg_Library_FooLib_foo/NOTICE"
        cases: dict[str, tuple[object, str]] = {
            "not a list": ({"path": "x"}, "x64: licenses is {'path': 'x'}, not a list"),
            "not in top": (
                [*FIXTURE_LIST, {"path": stray, "chosen": True}],
                f"x64: {stray} is not among the top-level licenses\nx64: {stray} is not a file in LICENSES/",
            ),
            "edk2 missing": ([bsd, gpl, bar, foo], "x64: edk2's licence LICENSES/edk2-License.txt is not listed, chosen"),
            "edk2 not chosen": (
                [{**edk2, "chosen": False}, bsd, gpl, bar, foo],
                "x64: edk2's licence LICENSES/edk2-License.txt is not listed, chosen\n"
                "x64: LICENSES/edk2-License.txt is not one licence of a choice, so it must be chosen",
            ),
            "both chosen": (
                [edk2, bsd, {**gpl, "chosen": True}, bar, foo],
                f"x64: {FDT} is dual-licensed: ['BSD-2-Clause', 'GPL'] must be listed with exactly "
                "['BSD-2-Clause'] chosen; chosen: ['BSD-2-Clause', 'GPL']",
            ),
            "none chosen": (
                [edk2, {**bsd, "chosen": False}, gpl, bar, foo],
                f"x64: {FDT} is dual-licensed: ['BSD-2-Clause', 'GPL'] must be listed with exactly "
                "['BSD-2-Clause'] chosen; chosen: []",
            ),
            "GPL chosen instead": (
                [edk2, {**bsd, "chosen": False}, {**gpl, "chosen": True}, bar, foo],
                f"x64: {FDT} is dual-licensed: ['BSD-2-Clause', 'GPL'] must be listed with exactly "
                "['BSD-2-Clause'] chosen; chosen: ['GPL']",
            ),
            "one of a choice unlisted": (
                [edk2, bsd, bar, foo],
                f"x64: {FDT}: LICENSES/MdePkg_Library_BaseFdtLib_libfdt/GPL is not listed",
            ),
            "a plain licence not chosen": (
                [edk2, bsd, gpl, {**bar, "chosen": False}, foo],
                f"x64: {bar['path']} is not one licence of a choice, so it must be chosen",
            ),
            "listed twice": ([*FIXTURE_LIST, foo], f"x64: {foo['path']} listed twice"),
            "out of order": (
                [edk2, bar, bsd, gpl, foo],
                f"x64: not in order (edk2's licence first, then by path): "
                f"{[edk2['path'], bar['path'], bsd['path'], gpl['path'], foo['path']]}",
            ),
            "a key more": (
                [*FIXTURE_LIST[:4], {**foo, "spdx": "MIT"}],
                f"x64: licence entry {({**foo, 'spdx': 'MIT'})!r} is not {{path, chosen}}",
            ),
            "chosen not a bool": (
                [*FIXTURE_LIST[:4], {**foo, "chosen": 1}],
                f"x64: licence entry {({**foo, 'chosen': 1})!r}: path not a string, or chosen not a bool",
            ),
            "escaping": (
                [edk2, {"path": "LICENSES/../manifest.json", "chosen": True}, bsd, gpl, bar, foo],
                "x64: LICENSES/../manifest.json is not among the top-level licenses\n"
                "x64: LICENSES/../manifest.json is not a file in LICENSES/",
            ),
        }
        for label, (listed, want) in cases.items():
            with self.subTest(label):
                self.assertEqual("\n".join(self._check(listed)), want)
        (self.stage / str(foo["path"])).unlink()
        self.assertEqual(self._check(FIXTURE_LIST), [f"x64: {foo['path']} is not a file in LICENSES/"])

    def test_check_against_parts(self) -> None:
        # A part compiled in but unlisted; one listed but not compiled in.
        self.assertEqual(
            self._check(FIXTURE_LIST, {FOO, BAR, FDT, UNUSED}),
            ["x64: LICENSES/PartPkg_Library_UnusedLib_unused/LICENSE.txt is not listed, but its part is compiled into the image"],
        )
        self.assertEqual(
            self._check(FIXTURE_LIST, {BAR, FDT}),
            ["x64: LICENSES/PartPkg_Library_FooLib_foo/LICENSE is listed, but its part is not in the image"],
        )
        self.assertEqual(
            self._check(FIXTURE_LIST, {FOO, BAR, FDT, "Gone/sub"}),
            ["x64: Gone/sub is in the image, but the release has no licence file of it"],
        )

    def test_report_shapes(self) -> None:
        # CRLF line ends; a module built twice (two sections, one INF); a
        # wrapped Module INF Path.
        twice = self.report_text.replace(
            "Module INF Path:      AppPkg/Dxe/Dxe.inf", "Module INF Path:      AppPkg/Dxe\n/Dxe.inf"
        )
        twin = (
            ">" + "=" * 118 + "<\nModule Summary\nModule Name:          Dxe\n"
            "Module INF Path:      AppPkg/Dxe/Dxe.inf\n" + "=" * 120 + "\n>" + "-" * 118 + "<\nLibrary\n" + "-" * 120 + "\n"
            f"{self.src}/PartPkg/Library/UnusedLib/UnusedLib.inf\n{{UnusedLib}}\n<" + "-" * 118 + ">\n<" + "=" * 118 + ">\n"
        )
        r = licenses.parse_report((twice + twin).replace("\n", "\r\n"))
        self.assertEqual(
            r.modules["AppPkg/Dxe/Dxe.inf"],
            [
                f"{self.src}/PartPkg/Library/FooLib/FooLib.inf",
                f"{self.overlay}/PartPkg/Library/BarLib/BarLib.inf",
                f"{self.src}/PartPkg/Library/IaLib/IaLib.inf",
                f"{self.src}/PartPkg/Library/UnusedLib/UnusedLib.inf",
            ],
        )
        self.assertEqual(
            licenses.image_parts(r, "FIXTURE_CODE", self.roots, self.subs, "X64"), ({FOO, BAR, FDT, UNUSED}, [])
        )

    def test_report_refused(self) -> None:
        lib = f"{self.src}/PartPkg/Library/UnusedLib/UnusedLib.inf"
        cases = {
            "a library path with no class line": (
                self.report_text.replace(f"{lib}\n{{UnusedLib:  Time = 4ms}}\n", f"{lib}\n"),
                f"AppPkg/Other/Other.inf: a library path with no class line: {lib}",
            ),
            "a class line with no path": (
                self.report_text.replace(f"{lib}\n", ""),
                "AppPkg/Other/Other.inf: a library class line with no path: {UnusedLib:  Time = 4ms}",
            ),
            "a volume the report could not read": (
                self.report_text.replace("Fv Name:            OTHERFV (1.0% Full)\n", ""),
                "FD FIXTURE_OTHER: an FV subsection with no 'Fv Name:': the report could not read the volume",
            ),
            "a volume with no entry": (
                self.report_text.replace(f"0x00000078 Other ({self.src}/AppPkg/Other/Other.inf)\n", ""),
                "FD FIXTURE_OTHER: an FV subsection with volume OTHERFV with no entry: the report could not read the volume",
            ),
        }
        for label, (text, want) in cases.items():
            with self.subTest(label), self.assertRaises(licenses.ReportError) as cm:
                licenses.parse_report(text)
            self.assertEqual(str(cm.exception), want)

    def test_inf_fields(self) -> None:
        inf = self.src / "PartPkg/Library/FooLib/FooLib.inf"
        inf.write_text(
            "[Defines]\n  BASE_NAME = FooLib\n\n[Sources]\n  foo/a.nasm ||||gFlag\n  foo/b.c | GCC | TAG\n"
            "  foo/c.c | MSFT | TAG\n  foo/d.h\n"
        )
        self.assertEqual(licenses.inf_sources(inf, "X64"), ["foo/a.nasm", "foo/b.c", "foo/d.h"])
        self.assertEqual(licenses.parts_of("PartPkg/Library/FooLib/FooLib.inf", self.roots, self.subs, "X64"), {FOO})
        # A header alone compiles nothing: no part.
        inf.write_text("[Defines]\n  BASE_NAME = FooLib\n\n[Sources]\n  FooLib.c\n  foo/d.h\n")
        self.assertEqual(licenses.parts_of("PartPkg/Library/FooLib/FooLib.inf", self.roots, self.subs, "X64"), set())

    def test_libtl(self) -> None:
        tl = "MdePkg/Library/ArmTransferListLib/libtl"
        d = licenses.licence_dir(tl)
        top = [licenses.EDK2_LICENSE, f"{d}/LICENSE-GPL-2.0-or-later.md", f"{d}/LICENSE-MIT.md", f"{d}/LICENSE.md"]
        listed = licenses.image_licenses({tl}, top)
        self.assertEqual(
            listed,
            [
                {"path": licenses.EDK2_LICENSE, "chosen": True},
                {"path": f"{d}/LICENSE-GPL-2.0-or-later.md", "chosen": False},
                {"path": f"{d}/LICENSE-MIT.md", "chosen": True},
                {"path": f"{d}/LICENSE.md", "chosen": True},
            ],
        )
        for path in top:
            (self.stage / path).parent.mkdir(parents=True, exist_ok=True)
            (self.stage / path).write_text(path)
        self.assertEqual(licenses.check_image_licenses("aarch64", listed, top, self.stage, {tl}), [])
        statement_off = [*listed[:3], {**listed[3], "chosen": False}]
        self.assertEqual(
            licenses.check_image_licenses("aarch64", statement_off, top, self.stage, {tl}),
            [f"aarch64: {d}/LICENSE.md is not one licence of a choice, so it must be chosen"],
        )
        self.assertEqual(
            licenses.missing_texts({tl, "Gone/sub"}, top), ["Gone/sub is in the image, but the release has no licence file of it"]
        )

    def _build(self) -> Path:
        """A stand-in build directory: modules, their libraries, objects' .deps."""
        b = self.root / "ws" / "Build" / "Fixture" / "DEBUG_GCC" / "X64"
        libs = {
            "FooLib": ("PartPkg/Library/FooLib", [self.src / FOO / "src/a.c", self.src / "PartPkg/Library/FooLib/FooLib.c"]),
            "BarLib": ("PartPkg/Library/BarLib", [self.overlay / BAR / "b.c"]),
            "BaseFdtLib": ("MdePkg/Library/BaseFdtLib", [self.src / FDT / "libfdt/fdt.c"]),
            "UnusedLib": ("PartPkg/Library/UnusedLib", [self.src / UNUSED / "u.c"]),
        }

        def objects(out: Path, sources: list[Path]) -> None:
            out.mkdir(parents=True, exist_ok=True)
            for i, source in enumerate(sources):
                (out / f"o{i}.obj.deps").write_text(f"{out}/o{i}.obj:  {source} \\\n {self.src}/MdePkg/Include/Base.h \\\n")

        for name, (d, sources) in libs.items():
            objects(b / d / name / "OUTPUT", sources)
        modules = {"Pei": ["BaseFdtLib"], "Dxe": ["FooLib", "BarLib"], "Other": ["UnusedLib"]}
        for name, linked in modules.items():
            mdir = b / "AppPkg" / name / name
            objects(mdir / "OUTPUT", [self.src / "AppPkg" / name / f"{name}.c"])
            (mdir / "GNUmakefile").write_text(f"MODULE_NAME = {name}\nMODULE_FILE = {name}.inf\n")
            lst = [f"{mdir}/OUTPUT/{name}.lib"] + [f"{b / libs[x][0] / x}/OUTPUT/{x}.lib" for x in linked]
            (mdir / "OUTPUT" / "static_library_files.lst").write_text("\n".join(lst) + "\n")
        return b

    def test_compiled_parts(self) -> None:
        b = self._build()
        r = licenses.parse_report(self.report_text)
        self.assertEqual(licenses.compiled_parts(r, "FIXTURE_CODE", b, self.roots, self.subs), ({FOO, BAR, FDT}, []))
        self.assertEqual(licenses.compiled_parts(r, "FIXTURE_OTHER", b, self.roots, self.subs), ({UNUSED}, []))
        # A FILE_GUID twin of Pei's build directory, linking another library.
        twin = b / "AppPkg" / "Pei" / "44444444-4444-4444-4444-444444444444"
        shutil.copytree(b / "AppPkg" / "Pei" / "Pei", twin)
        (twin / "OUTPUT" / "static_library_files.lst").write_text(
            f"{b}/PartPkg/Library/UnusedLib/UnusedLib/OUTPUT/UnusedLib.lib\n"
        )
        self.assertEqual(
            licenses.compiled_parts(r, "FIXTURE_CODE", b, self.roots, self.subs), ({FOO, BAR, FDT, UNUSED}, [])
        )
        (twin / "OUTPUT" / "static_library_files.lst").unlink()
        (twin / "OUTPUT" / "o0.obj").write_bytes(b"")
        (b / "PartPkg/Library/FooLib/FooLib/OUTPUT/o0.obj").write_bytes(b"")
        (b / "PartPkg/Library/BarLib/BarLib/OUTPUT/o9.obj").write_bytes(b"")
        self.assertEqual(
            licenses.compiled_parts(r, "FIXTURE_CODE", b, self.roots, self.subs),
            (
                {FOO, BAR, FDT},
                [
                    f"Pei: {twin}/OUTPUT/static_library_files.lst missing, so its libraries are unknown",
                    f"{b}/PartPkg/Library/BarLib/BarLib/OUTPUT/o9.obj: no .deps, so its source is unknown",
                ],
            ),
        )
        shutil.rmtree(twin)
        shutil.rmtree(b / "AppPkg" / "Pei")
        (b / "PartPkg/Library/BarLib/BarLib/OUTPUT/o9.obj").unlink()
        self.assertEqual(
            licenses.compiled_parts(r, "FIXTURE_CODE", b, self.roots, self.subs),
            ({FOO, BAR}, [f"Pei: no build directory for AppPkg/Pei/Pei.inf in {b}"]),
        )

    def test_derivation_sabotaged(self) -> None:
        # The derivation (report and [Sources]) missing a part is caught by
        # the objects' route: one INF's mapping into its submodule dropped.
        b = self._build()
        r = licenses.parse_report(self.report_text)
        real = licenses.inf_sources

        def dropped(inf: Path, arch: str) -> list[str]:
            return [x for x in real(inf, arch) if not x.startswith("foo/")]

        compiled, _ = licenses.compiled_parts(r, "FIXTURE_CODE", b, self.roots, self.subs)
        with mock.patch.object(licenses, "inf_sources", dropped):
            parts, _ = licenses.image_parts(r, "FIXTURE_CODE", self.roots, self.subs, "X64")
        listed = licenses.image_licenses(parts, FIXTURE_TOP)
        self.assertEqual(
            self._check(listed, compiled),
            ["x64: LICENSES/PartPkg_Library_FooLib_foo/LICENSE is not listed, but its part is compiled into the image"],
        )


def _elf(path: Path, symbols: list[tuple[str, int, int, int]]) -> None:
    """A stand-in ELF64: a .text and a symbol table of (name, type, value, size)."""
    strtab = b"\0"
    syms = b"\0" * 24
    for name, kind, value, size in symbols:
        syms += struct.pack("<IBBHQQ", len(strtab), kind, 0, 1, value, size)
        strtab += name.encode() + b"\0"
    text = b"\x90" * 16
    body = text + syms + strtab
    shoff = 64 + len(body)
    head = b"\x7fELF\x02\x01\x01" + b"\0" * 9 + struct.pack("<HHIQQQIHHHHHH", 1, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 4, 0)
    sections = [
        struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        struct.pack("<IIQQQQIIQQ", 0, 1, 6, 0, 64, len(text), 0, 0, 16, 0),
        struct.pack("<IIQQQQIIQQ", 0, 2, 0, 0, 64 + len(text), len(syms), 3, 1, 8, 24),
        struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, 64 + len(text) + len(syms), len(strtab), 0, 0, 1, 0),
    ]
    path.write_bytes(head + body + b"".join(sections))


# A stand-in objdump: prints the file <elf>.dis beside the ELF it is given.
_OBJDUMP = """#!/usr/bin/env python3
import sys
from pathlib import Path
dis = Path(sys.argv[-1] + ".dis")
if not dis.is_file():
    sys.exit(f"objdump: {sys.argv[-1]}: no stand-in output")
sys.stdout.write(dis.read_text())
"""
FUNC, OBJECT = 2, 1


class IsaTests(unittest.TestCase):
    """The X64 instruction-set check: baseline x86-64 only (isa.py)."""

    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.objdump = self.root / "objdump"
        self.objdump.write_text(_OBJDUMP)
        self.objdump.chmod(0o755)

    def tearDown(self) -> None:
        self._t.cleanup()

    def test_extensions(self) -> None:
        # The whole table, exactly: the x86-64 psABI's v2 and v3 extensions
        # (but AVX, AVX2, FMA and F16C, which are VEX), and those in no level.
        want = {
            ("CMPXCHG16B", "v2"): "cmpxchg16b",
            ("LAHF-SAHF", "v2"): "lahf sahf",
            ("POPCNT", "v2"): "popcnt",
            ("SSE3", "v2"): "addsubpd addsubps haddpd haddps hsubpd hsubps lddqu movddup movshdup movsldup fisttp",
            ("SSSE3", "v2"): "pabsb pabsw pabsd palignr phaddw phaddd phaddsw phsubw phsubd phsubsw pmaddubsw"
            " pmulhrsw pshufb psignb psignw psignd",
            ("SSE4.1", "v2"): "blendpd blendps blendvpd blendvps dppd dpps extractps insertps movntdqa mpsadbw"
            " packusdw pblendvb pblendw pcmpeqq pextrb pextrd pextrq phminposuw pinsrb pinsrd pinsrq pmaxsb"
            " pmaxsd pmaxud pmaxuw pminsb pminsd pminud pminuw pmovsxbw pmovsxbd pmovsxbq pmovsxwd pmovsxwq"
            " pmovsxdq pmovzxbw pmovzxbd pmovzxbq pmovzxwd pmovzxwq pmovzxdq pmuldq pmulld ptest roundpd"
            " roundps roundsd roundss",
            ("SSE4.2", "v2"): "crc32 pcmpestri pcmpestrm pcmpistri pcmpistrm pcmpgtq",
            ("BMI1", "v3"): "andn bextr blsi blsmsk blsr tzcnt",
            ("BMI2", "v3"): "bzhi mulx pdep pext rorx sarx shlx shrx",
            ("LZCNT", "v3"): "lzcnt",
            ("MOVBE", "v3"): "movbe",
            ("AES-NI", "no level"): "aesenc aesenclast aesdec aesdeclast aesimc aeskeygenassist",
            ("PCLMULQDQ", "no level"): "pclmulqdq",
            ("SHA", "no level"): "sha1rnds4 sha1nexte sha1msg1 sha1msg2 sha256rnds2 sha256msg1 sha256msg2",
            ("ADX", "no level"): "adcx adox",
            ("GFNI", "no level"): "gf2p8affineqb gf2p8affineinvqb gf2p8mulb",
            ("RDSEED", "no level"): "rdseed",
        }
        self.assertEqual(isa.EXTENSIONS, {m: ext for ext, names in want.items() for m in names.split()})

    def test_classify(self) -> None:
        want = {
            "MOVBE": ("MOVBE", "v3"),
            "vmovdqu": isa.VECTOR,
            "vfmadd231ps": isa.VECTOR,
            "vcvtph2ps": isa.VECTOR,
            "vzeroupper": isa.VECTOR,
            "vpternlogd": isa.VECTOR,
            "kmovw": isa.MASK,
            "kortestq": isa.MASK,
            "kunpckbw": isa.MASK,
        }
        for m, ext in want.items():
            with self.subTest(m):
                self.assertEqual(isa.classify(m), ext)
        # Baseline x86-64 (SSE2 included), and what begins with v but is not VEX.
        for m in (
            "mov", "bsf", "bsr", "stos", "cmpxchg8b", "pextrw", "pinsrw", "pshufd", "movdqu", "paddq",
            "rdrand", "xsave", "xgetbv", "monitor", "mwait", "endbr64", "verr", "verw", "vmcall",
            "vmmcall", "vmgexit", "vmlaunch", "vmxon", "kill", "k",
        ):  # fmt: skip
            with self.subTest(m):
                self.assertIsNone(isa.classify(m))

    def test_instruction(self) -> None:
        cases = {
            "{vex} vpdpbusd ymm0,ymm1,ymm2": ("vpdpbusd", isa.VECTOR),
            "{evex} vpaddd zmm0,zmm1,zmm2": ("vpaddd", isa.VECTOR),
            "pextrw WORD PTR [rdi],xmm0,0x3": ("pextrw", ("SSE4.1", "v2")),
            "pextrw eax,xmm0,0x3": ("pextrw", None),
            "pinsrw xmm0,WORD PTR [rdi],0x3": ("pinsrw", None),
            "aesenc xmm0,xmm1": ("aesenc", ("AES-NI", "no level")),
            "shrx   rax,rax,rsi": ("shrx", ("BMI2", "v3")),
            "mov    eax,DWORD PTR [rip+0x10]": ("mov", None),
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual(isa.instruction(text), want)

    def test_mnemonic(self) -> None:
        cases = {
            "lock cmpxchg16b OWORD PTR [rdi]": "cmpxchg16b",
            "rep stos BYTE PTR es:[rdi],al": "stos",
            "rex.W andn rax,rsi,rdi": "andn",
            "notrack jmp rax": "jmp",
            "cs nop WORD PTR [rax+rax*1+0x0]": "nop",
            "data16 cs nop WORD PTR [rax+rax*1+0x0]": "nop",
            "shrx   rax,rax,rsi": "shrx",
            "{vex} vpdpbusd ymm0,ymm1,ymm2": "vpdpbusd",
            "": "",
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual(isa.mnemonic(text), want)

    def test_functions(self) -> None:
        elf = self.root / "m.debug"
        _elf(elf, [("Entry", FUNC, 0x240, 0x10), ("Data", OBJECT, 0x300, 8), ("Label", FUNC, 0x400, 0), ("A", FUNC, 0x200, 4)])
        self.assertEqual(isa.functions(elf.read_bytes()), [(0x200, 0x204, "A"), (0x240, 0x250, "Entry")])
        _elf(elf, [("Data", OBJECT, 0x300, 8)])
        with self.assertRaises(isa.ElfError) as cm:
            isa.functions(elf.read_bytes())
        self.assertEqual(str(cm.exception), "no function symbols (a stripped file?)")
        with self.assertRaises(isa.ElfError) as cm:
            isa.functions(b"MZ" + b"\0" * 62)
        self.assertEqual(str(cm.exception), "not a 64-bit little-endian ELF file")
        _elf(elf, [("A", FUNC, 0x200, 4)])
        with self.assertRaises(isa.ElfError) as cm:
            isa.functions(elf.read_bytes()[:-100])
        self.assertEqual(str(cm.exception), "an unreadable section or symbol table (287 bytes)")
        with self.assertRaises(isa.ElfError) as cm:
            isa.functions(b"\x7fELF")
        self.assertEqual(str(cm.exception), "not a 64-bit little-endian ELF file")
        # A function symbol inside another: one range, the outer's.
        _elf(elf, [("Outer", FUNC, 0x200, 0x40), ("Inner", FUNC, 0x210, 8), ("Next", FUNC, 0x240, 4)])
        self.assertEqual(isa.functions(elf.read_bytes()), [(0x200, 0x240, "Outer"), (0x240, 0x244, "Next")])

    DIS = (
        "\nm.debug:     file format elf64-x86-64\n\n\nDisassembly of section .text:\n\n"
        "0000000000000240 <Entry>:\n"
        " 240:\tpush   rbp\n"
        " 241:\tandn   rax,rsi,rdi\n"
        " 246:\tlock cmpxchg16b OWORD PTR [rdi]\n"
        " 24b:\tvmovdqu ymm0,YMMWORD PTR [rsi]\n"
        " 24f:\tret\n"
        # Data in .text (edk2's linker script puts .rodata there): no function.
        " 250:\tvpaddd ymm1,ymm2,ymm3\n"
        " 254:\t(bad)\n"
        "0000000000000260 <Other>:\n"
        " 260:\tpopcnt eax,edi\n"
        "\t...\n"
    )

    def test_scan_text(self) -> None:
        funcs = [(0x240, 0x250, "Entry"), (0x260, 0x264, "Other")]
        scan = isa.scan_text(self.DIS, funcs)
        self.assertEqual((scan.functions, scan.instructions), (2, 6))
        self.assertEqual(
            scan.hits,
            [
                isa.Hit(0x241, "andn", "BMI1", "v3", "Entry"),
                isa.Hit(0x246, "cmpxchg16b", "CMPXCHG16B", "v2", "Entry"),
                isa.Hit(0x24B, "vmovdqu", *isa.VECTOR, "Entry"),
                isa.Hit(0x260, "popcnt", "POPCNT", "v2", "Other"),
            ],
        )
        self.assertEqual(
            isa.summary(scan.hits),
            "AVX, AVX2, FMA, F16C or AVX-512 (VEX/EVEX) (v3): 1, first vmovdqu at 0x24b in Entry; "
            "BMI1 (v3): 1, first andn at 0x241 in Entry; CMPXCHG16B (v2): 1, first cmpxchg16b at 0x246 in Entry; "
            "POPCNT (v2): 1, first popcnt at 0x260 in Other",
        )
        # The same code, outside every function: nothing read.
        self.assertEqual(isa.scan_text(self.DIS, [(0x100, 0x200, "Far")]), isa.Scan(functions=1))

    def _module(self, b: Path, name: str, cc: str, dis: str | None, libs: dict[str, str]) -> None:
        """A stand-in module build directory: its makefile, ELF and libraries."""
        mod = b / "AppPkg" / name / name
        (mod / "OUTPUT").mkdir(parents=True)
        (mod / "DEBUG").mkdir()
        (mod / "GNUmakefile").write_text(f"MODULE_NAME = {name}\nMODULE_FILE = {name}.inf\nCC_FLAGS = {cc}\n")
        lst = [f"{mod}/OUTPUT/{name}.lib"]
        for lib, lib_cc in libs.items():
            d = b / "AppPkg" / "Library" / lib / lib
            (d / "OUTPUT").mkdir(parents=True, exist_ok=True)
            (d / "GNUmakefile").write_text(f"MODULE_NAME = {lib}\nCC_FLAGS = {lib_cc}\n")
            lst.append(f"{d}/OUTPUT/{lib}.lib")
        (mod / "OUTPUT" / "static_library_files.lst").write_text("\n".join(lst) + "\n")
        if dis is not None:
            _elf(mod / "DEBUG" / f"{name}.debug", [("Entry", FUNC, 0x240, 0x10), ("Other", FUNC, 0x260, 4)])
            (mod / "DEBUG" / f"{name}.debug.dis").write_text(dis)

    def _baseline(self, b: Path, names: list[str]) -> tuple[dict[str, int], list[str]]:
        report = licenses.Report(
            fds={"CODE": [licenses.Volume("FV", ["PEI Apriori", *[f"{n} (/src/AppPkg/{n}/{n}.inf)" for n in names]])]}
        )
        return isa.check_baseline(report, "CODE", b, [Path("/src")], str(self.objdump))

    def test_check_baseline(self) -> None:
        b = self.root / "X64"
        clean = " 240:\tpush   rbp\n 241:\tret\n 260:\tbsf    eax,edi\n"
        flags = "-Os -m64 -march=x86-64-v3 -mno-sse -march=x86-64 -flto"
        self._module(b, "Sec", flags, clean, {"BaseLib": flags, "IoLib": flags})
        self._module(b, "Dxe", flags, clean, {"BaseLib": flags})
        # NASM straight to a binary: no ELF, no object.
        self._module(b, "Reset", "-m64", None, {})
        (b / "AppPkg/Reset/Reset/OUTPUT/static_library_files.lst").unlink()
        names = ["Sec", "Dxe", "Reset"]
        counts = {"modules": 2, "without_code": 1, "libraries": 2, "functions": 4, "instructions": 6}
        self.assertEqual(self._baseline(b, names), (counts, []))

        sec = b / "AppPkg/Sec/Sec"
        lib = b / "AppPkg/Library/IoLib/IoLib/GNUmakefile"
        (sec / "DEBUG/Sec.debug.dis").write_text(self.DIS)
        (sec / "GNUmakefile").write_text("MODULE_NAME = Sec\nMODULE_FILE = Sec.inf\nCC_FLAGS = -Os -m64 -flto\n")
        lib.write_text("MODULE_NAME = IoLib\nCC_FLAGS = -march=x86-64 -march=x86-64-v3\n")
        base = b / "AppPkg/Library/BaseLib/BaseLib/GNUmakefile"
        base.write_text("MODULE_NAME = BaseLib\nCC_FLAGS = -msse2 -mno-sse -march=x86-64 -mbmi2 -maes\n")
        (b / "AppPkg/Reset/Reset/OUTPUT/Reset.obj").write_bytes(b"")
        self.assertEqual(
            self._baseline(b, names),
            (
                {**counts, "without_code": 0, "instructions": 9},
                [
                    f"Sec: {sec}/GNUmakefile compiles with no -march, not -march=x86-64",
                    f"Sec's library: {base} enables extensions beyond -march=x86-64: -mbmi2 -maes",
                    f"Sec's library: {lib} compiles with -march=x86-64-v3, not -march=x86-64",
                    f"Sec: 4 instructions beyond x86-64 in {sec}/DEBUG/Sec.debug: {isa.summary(isa.scan_text(self.DIS, [(0x240, 0x250, 'Entry'), (0x260, 0x264, 'Other')]).hits)}",
                    f"Reset: {b}/AppPkg/Reset/Reset/DEBUG/Reset.debug missing, though the module compiled objects",
                ],
            ),
        )
        # An objdump that fails, and one that prints no instruction.
        (sec / "DEBUG/Sec.debug.dis").unlink()
        shutil.rmtree(b / "AppPkg/Reset")
        elf = sec / "DEBUG/Sec.debug"
        self.assertEqual(
            self._baseline(b, ["Sec"])[1][3:],
            [
                f"Sec: {elf}: {self.objdump} failed (status 1): objdump: {elf}: no stand-in output",
                "FD CODE: no module scanned",
            ],
        )
        (sec / "DEBUG/Sec.debug.dis").write_text("\nDisassembly of section .text:\n")
        self.assertEqual(
            self._baseline(b, ["Sec"])[1][3:],
            [f"Sec: {elf}: {self.objdump}'s output has no instruction inside a function", "FD CODE: no module scanned"],
        )
        self.assertEqual(
            self._baseline(b, []), ({**counts, "modules": 0, "without_code": 0, "libraries": 0, "functions": 0, "instructions": 0}, ["FD CODE: no module scanned"])
        )

    def test_release_isa_checks_reads_each_x64_report(self) -> None:
        # Each X64 image set's own report, FD and build; AARCH64's reports
        # (not reports at all here) are never read.
        ws, src = self.root / "ws", self.root / "src"
        ws.mkdir()
        text = (FIXTURES / "licenses" / "report.txt").read_text()
        for target in ("DEBUG", "RELEASE", "GUARDED"):
            (ws / f"report-X64-{target}.txt").write_text(text)
            (ws / f"report-AARCH64-{target}.txt").write_text("not a report")
        calls: list[tuple[str, Path, list[Path]]] = []

        def fake(
            report: licenses.Report, fd: str, arch_build: Path, roots: list[Path], objdump: str = "objdump"
        ) -> tuple[dict[str, int], list[str]]:
            calls.append((fd, arch_build, roots))
            return {"modules": len(calls)}, [f"problem {len(calls)}"]

        with mock.patch.object(isa, "check_baseline", fake):
            got = release.isa_checks(ws, src, "GCC")
        self.assertEqual(
            got,
            (
                {"x64": {"modules": 1}, "release/x64": {"modules": 2}, "guarded/x64": {"modules": 3}},
                ["x64: problem 1", "release/x64: problem 2", "guarded/x64: problem 3"],
            ),
        )
        self.assertEqual(
            calls,
            [
                ("OVMF_CODE", ws / "Build/OvmfX64/DEBUG_GCC/X64", [ws / "overlay", src]),
                ("OVMF_CODE", ws / "Build/OvmfX64/RELEASE_GCC/X64", [ws / "overlay", src]),
                ("OVMF_CODE", ws / "guarded/Build/OvmfX64/DEBUG_GCC/X64", [ws / "overlay", src]),
            ],
        )

    def test_assemble_stops_on_an_isa_problem(self) -> None:
        # The ISA check's problems stop assemble before any boot.
        a = release.AssembleArgs(
            ws=self.root / "ws", src=self.root / "src", dist=self.root / "dist", stage=self.root / "stage",
            release="r", tag="t", commit="c", url="u", fixes=[], toolchain="GCC", release_date="01/01/2026",
            compilers={}, defines={}, pcds={}, patches=[], repo_commit="none", repo_dirty=0, built_by="local",
            qemu_dir=None,
        )  # fmt: skip
        err = io.StringIO()
        with (
            mock.patch.object(release, "stage_images", return_value=[]),
            mock.patch.object(release, "static_checks", return_value=({}, [])),
            mock.patch.object(release, "volinfo_checks", return_value=[]),
            mock.patch.object(release, "isa_checks", return_value=({}, ["x64: beyond"])),
            mock.patch.object(checks, "build_facts", return_value=({}, [])),
            mock.patch.object(checks, "check_patches", return_value=[]),
            mock.patch.object(release, "boot_checks", side_effect=AssertionError("booted")),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(release.assemble(a), 1)
        self.assertEqual(err.getvalue(), "FAIL: x64: beyond\n")

    def test_release_isa_checks(self) -> None:
        # Each X64 image set, from its own report; AARCH64 is not read.
        ws = self.root / "ws"
        ws.mkdir()
        counts, problems = release.isa_checks(ws, self.root / "src", "GCC")
        self.assertEqual(counts, {})
        self.assertEqual(
            problems,
            [
                f"x64: the build report {ws}/report-X64-DEBUG.txt: [Errno 2] No such file or directory: '{ws}/report-X64-DEBUG.txt'",
                f"release/x64: the build report {ws}/report-X64-RELEASE.txt: [Errno 2] No such file or directory: "
                f"'{ws}/report-X64-RELEASE.txt'",
                f"guarded/x64: the build report {ws}/report-X64-GUARDED.txt: [Errno 2] No such file or directory: "
                f"'{ws}/report-X64-GUARDED.txt'",
            ],
        )

    def test_host_compiler(self) -> None:
        # The scan on real code: the host's gcc builds one file for each
        # level, and the host's objdump reads it. gcc's own default may be
        # any level (Ubuntu's amd64v3 gcc: x86-64-v3), so each is named.
        gcc, objdump = shutil.which("gcc"), shutil.which("objdump")
        if gcc is None or objdump is None or os.uname().machine != "x86_64":
            self.skipTest("SKIP: needs an x86_64 host with gcc and objdump")
        src = self.root / "t.c"
        src.write_text(
            "unsigned long long Mask(unsigned long long a, unsigned long long b) { return (a & ~b) >> (b & 63); }\n"
            "int Count(unsigned long long x) { return __builtin_popcountll(x); }\n"
        )
        got: dict[str, set[str]] = {}
        for level in ("x86-64", "x86-64-v2", "x86-64-v3"):
            obj = self.root / f"{level}.o"
            subprocess.run([gcc, "-Os", f"-march={level}", "-c", str(src), "-o", str(obj)], check=True)
            got[level] = {f"{h.mnemonic} {h.level}" for h in isa.scan_elf(obj, objdump).hits}
        self.assertEqual(
            got, {"x86-64": set(), "x86-64-v2": {"popcnt v2"}, "x86-64-v3": {"andn v3", "shrx v3", "popcnt v2"}}
        )


VOLINFO = FIXTURES / "volinfo"


def _volinfo_text(name: str) -> str:
    path = VOLINFO / name
    if path.suffix == ".xz":
        return lzma.decompress(path.read_bytes()).decode()
    return path.read_text()


def _walker_guids(name: str) -> set[str]:
    lines = (VOLINFO / f"{name}-walker-guids.txt").read_text().splitlines()
    return {x for x in lines if x and not x.startswith("#")}


def _fv_header(length: int) -> bytes:
    """An EFI_FIRMWARE_VOLUME_HEADER's first 50 bytes: FvLength, _FVH, HeaderLength."""
    return b"\0" * 32 + struct.pack("<Q", length) + b"_FVH" + b"\0" * 4 + struct.pack("<H", 0x48)


class VolInfoTests(unittest.TestCase):
    """The second reading of an image, by edk2's VolInfo, and its comparison."""

    def test_parse_real_output(self) -> None:
        # Captured from r2's RELEASE images with the build's own VolInfo:
        # X64 has two top-level volumes (FVMAIN_COMPACT, SECFV), AARCH64 one.
        x64 = volinfo.parse(_volinfo_text("release-x64-0x0.txt.xz")) | volinfo.parse(
            _volinfo_text("release-x64-0x348000.txt")
        )
        self.assertEqual(x64, _walker_guids("release-x64"))
        self.assertEqual(len(x64), 150)
        aa64 = volinfo.parse(_volinfo_text("release-aarch64-0x1000.txt.xz"))
        self.assertEqual(aa64, _walker_guids("release-aarch64"))
        self.assertEqual(len(aa64), 123)
        self.assertNotIn(volinfo.FREE_SPACE, x64 | aa64)

    def test_chatter(self) -> None:
        # The decompressor's "Decoding" lands inside a line; cut out, the
        # line is whole again.
        text = _volinfo_text("release-x64-0x0.txt.xz")
        plain = text.replace("Decoding\n", "")
        i = plain.index("File Name:", plain.index("SectionDefinitionGuid:"))
        moved = plain[:i] + "File Na" + "Decoding\n" + plain[i + 7 :]
        self.assertEqual(volinfo.parse(moved), volinfo.parse(text))
        self.assertEqual(len(volinfo.parse(text)), 148)

    def test_parse_refuses(self) -> None:
        text = _volinfo_text("release-x64-0x348000.txt")
        lines = text.splitlines()
        first = next(i for i, x in enumerate(lines) if x.startswith("File Name:"))
        cases = {
            "an unknown line": (
                text.replace("File Type:", "File Kind:", 1),
                "VolInfo output line {n} not understood: {line!r}",
            ),
            "a file dropped": (
                "\n".join(lines[:first] + lines[first + 1 :]),
                "VolInfo output names 3 files, but its totals add up to 4",
            ),
            "cut short": (
                "\n".join(lines[: first + 3]),
                "VolInfo output states 0 volume totals, not one ('files in this FV')",
            ),
            "nothing": ("", "VolInfo output states 0 volume totals, not one ('files in this FV')"),
        }
        # A decode that failed in silence: VolInfo exits 0 with an empty
        # encapsulation, no "Decoding", and totals that still add up.
        big = _volinfo_text("release-x64-0x0.txt.xz")
        start = big.index("/------------ Encapsulation section start")
        start = big.index("\n", start) + 1
        end = big.index("\\------------ Encapsulation section end")
        nested = big[start:end]
        hollow = big[:start] + big[end:]
        total = big.count("File Name:") - nested.count("File Name:")
        hollow = hollow.replace("Decoding\n", "")
        self.assertIn("There are a total of", hollow)
        with self.assertRaises(volinfo.VolInfoError) as cm:
            volinfo.parse(hollow)
        self.assertRegex(str(cm.exception), r"^VolInfo output line \d+: an empty encapsulated section \(its decoding failed\)$")
        self.assertGreater(total, 0)
        with self.assertRaises(volinfo.VolInfoError) as cm:
            volinfo.parse(big.replace("Decoding\n", ""))
        self.assertEqual(str(cm.exception), "VolInfo output has 1 LZMA sections but 0 decoded")
        bad = next(i for i, x in enumerate(lines) if x.startswith("File Type:"))
        for label, (t, want) in cases.items():
            with self.subTest(label), self.assertRaises(volinfo.VolInfoError) as cm:
                volinfo.parse(t)
            self.assertEqual(str(cm.exception), want.format(n=bad + 1, line=lines[bad].replace("File Type:", "File Kind:")))

    def test_volume_offsets(self) -> None:
        data = _fv_header(0x100) + b"\xff" * (0x100 - 50) + b"pad" + _fv_header(0x80) + b"\0" * (0x80 - 50)
        self.assertEqual(volinfo.volume_offsets(data), [0, 0x103])
        # A volume longer than the file is no volume.
        self.assertEqual(volinfo.volume_offsets(_fv_header(0x1000)), [])

    def _fake(self, root: Path, script: str) -> tuple[Path, Path, Path]:
        """A stand-in VolInfo, an image of two volumes, a tools file."""
        bin_dir = root / "BaseTools" / "BinWrappers" / "PosixLike"
        bin_dir.mkdir(parents=True)
        tool = bin_dir / "VolInfo"
        tool.write_text("#!/usr/bin/env bash\n" + script)
        tool.chmod(0o755)
        image = root / "code.fd"
        image.write_bytes(_fv_header(0x100) + b"\0" * (0x100 - 50) + _fv_header(0x80) + b"\0" * (0x80 - 50))
        tools = root / "GuidedSectionTools.txt"
        tools.write_text("EE4E5898-3914-4259-9D6E-DC7BD79403CF LZMA LzmaCompress\n")
        return image, tools, bin_dir

    def test_image_guids(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "0x0").write_text(_volinfo_text("release-x64-0x0.txt.xz"))
            (root / "0x100").write_text(_volinfo_text("release-x64-0x348000.txt"))
            # It must find the tools file beside the image it reads.
            image, tools, bin_dir = self._fake(
                root, f'[[ $1 == --offset && -f "$(dirname "$3")/GuidedSectionTools.txt" ]] || exit 9\ncat {root}/"$2"\n'
            )
            self.assertEqual(volinfo.image_guids(image, tools, bin_dir), _walker_guids("release-x64"))
            # The caller's EDK_TOOLS_PATH would make edk2's wrappers run a
            # host copy: the workspace's is passed instead.
            image, tools, bin_dir = self._fake(
                root / "w", f'[[ $EDK_TOOLS_PATH == {root}/w/BaseTools ]] || {{ echo "host $EDK_TOOLS_PATH"; exit 3; }}\ncat {root}/"$2"\n'
            )
            with mock.patch.dict(os.environ, {"EDK_TOOLS_PATH": "/opt/host-edk2/BaseTools"}):
                self.assertEqual(volinfo.image_guids(image, tools, bin_dir), _walker_guids("release-x64"))

    def test_image_guids_refused(self) -> None:
        cases = {
            # As VolInfo does: Error() writes to stdout, and the status is 2.
            "exits 2": (
                "echo 'VolInfo: ERROR 0003: error parsing FV image'\nexit 2\n",
                "exited 2: ",
            ),
            "warns": ("echo 'EFI_SECTION_GUID_DEFINED cannot be parsed at this time' >&2\n", "exited 0: EFI_SECTION_GUID_DEFINED cannot be parsed at this time"),
            "unparseable": ("echo garbage\n", "VolInfo output line 1 not understood: 'garbage'"),
        }
        for label, (script, tail) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as t:
                image, tools, bin_dir = self._fake(Path(t), script)
                with self.assertRaises(volinfo.VolInfoError) as cm:
                    volinfo.image_guids(image, tools, bin_dir)
                self.assertEqual(str(cm.exception), f"{image}: VolInfo at 0x0: {tail}" if label == "unparseable" else f"{image}: VolInfo at 0x0 {tail}")

    def test_compare(self) -> None:
        both = _walker_guids("release-aarch64")
        one = sorted(both)[0]
        image = Path("/r/aarch64/code.fd")
        self.assertEqual(volinfo.compare(image, both, set(both)), [])
        self.assertEqual(
            volinfo.compare(image, both, both - {one}),
            [f"{image}: the FV walker finds files VolInfo does not: {one}"],
        )
        self.assertEqual(
            volinfo.compare(image, both - {one}, both),
            [f"{image}: VolInfo finds files the FV walker does not: {one}"],
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
    guarded: bool = True,
) -> Path:
    """A small unpacked release shaped as a real one; its top directory.

    tamper edits the manifest before it is written; without guarded, the
    release is r2's shape, with no GUARDED sets.
    """
    tag, commit = "edk2-stable202611", "556dcfb466e9f05affeaff881d738807f8156d3c"
    stage = Path(tempfile.mkdtemp(dir=root, prefix=f"stage-{rel}-"))
    top = stage / f"axl-firmware-{rel}"
    images: dict[str, object] = {}
    (top / "descriptors").mkdir(parents=True)
    for img in release.IMAGES if guarded else release.DESCRIBED:
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
            "licenses": [{"path": "LICENSES/edk2-License.txt", "chosen": True}],
        }
        if img.described:
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
    (top / "LICENSES").mkdir()
    (top / "LICENSES" / "edk2-License.txt").write_text("edk2 licence")
    manifest: Manifest = {
        "format": fmt,
        "release": rel,
        "edk2": {"tag": tag, "commit": commit, "patches": [{"name": n, "sha256": "0" * 64} for n in checks.REQUIRED_PATCHES]},
        "default_flavour": "DEBUG",
        "hardware_images": {"x64": "release/x64", "aarch64": "release/aarch64"},
        "images": images,
        "descriptors": [f"descriptors/{img.descriptor(rel)}" for img in release.DESCRIBED],
        "licenses": ["LICENSES/edk2-License.txt"],
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
    guarded: bool = True,
) -> Path:
    """A small release tarball shaped as a real one, and its SHA256SUMS."""
    top = _fake_tree(
        root, rel, fmt=fmt, outside=outside, marker=marker, tamper=tamper, swap_descriptor=swap_descriptor,
        guarded=guarded,
    )
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
        # The guarded sets are installed and checked, with no descriptor.
        self.assertEqual(
            (self.prefix / rel / "guarded/aarch64/Shell_AARCH64.efi").read_bytes(),
            f"{rel} guarded/aarch64 Shell_AARCH64.efi".encode(),
        )
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

    def test_without_guarded_sets(self) -> None:
        # r2's shape: the four sets only.
        r2 = _fake_release(self.root, "edk2-stable202611-r2", guarded=False)
        install.install("o/r", str(r2), self.prefix, None, symbols=False)
        self.assertEqual(
            sorted(p.name for p in (self.prefix / "edk2-stable202611-r2").iterdir()),
            ["LICENSES", "aarch64", "descriptors", "manifest.json", "release", "x64"],
        )
        self.assertEqual(len(self._descriptors()), 4)

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

        def guarded_moved(m: Manifest) -> None:
            images = cast("dict[str, dict[str, object]]", m["images"])
            images["guarded/x64"]["code"] = images["x64"]["code"]

        def guarded_digest(m: Manifest) -> None:
            images = cast("dict[str, dict[str, dict[str, object]]]", m["images"])
            images["guarded/aarch64"]["vars"]["sha256"] = "0" * 64

        def escaping_set(m: Manifest) -> None:
            images = cast("dict[str, object]", m["images"])
            images["../x64"] = images["x64"]

        def unknown_arch(m: Manifest) -> None:
            images = cast("dict[str, object]", m["images"])
            images["guarded/riscv64"] = images["guarded/x64"]

        want = ["aarch64", "release/aarch64", "release/x64", "x64"]
        have = sorted([*want, "guarded/aarch64", "guarded/x64"])
        rel = "edk2-stable202611-r1"
        names = sorted(f"descriptors/{release.Image(a, f).descriptor(rel)}" for f in ("DEBUG", "RELEASE") for a in ("x64", "aarch64"))
        cases: dict[str, tuple[Callable[[Manifest], None], str]] = {
            "no descriptors": (no_descriptors, f"its manifest's descriptors are [], not {names}"),
            "moved": (moved, "its manifest names 'release/x64/code.fd' for x64's code, not 'x64/code.fd'"),
            "partial": (
                partial,
                f"its manifest's images are {[x for x in have if x != 'release/aarch64']}, without ['release/aarch64']",
            ),
            "empty": (empty, f"its manifest's images are [], without {want}"),
            "guarded moved": (
                guarded_moved, "its manifest names 'x64/code.fd' for guarded/x64's code, not 'guarded/x64/code.fd'"
            ),
            "escaping set": (escaping_set, "its manifest lists an image set '../x64', not <name>/<x64|aarch64>"),
            "unknown arch": (
                unknown_arch, "its manifest lists an image set 'guarded/riscv64', not <name>/<x64|aarch64>"
            ),
        }
        for label, (tamper, tail) in cases.items():
            with self.subTest(label):
                bad = _fake_release(self.root, "edk2-stable202611-r1", tamper=tamper)
                with self.assertRaises(install.InstallError) as cm:
                    install.install("o/r", str(bad), self.prefix, None, symbols=False)
                self.assertTrue(str(cm.exception).endswith(f"/axl-firmware-edk2-stable202611-r1: {tail}"), str(cm.exception))
                self.assertEqual(os.listdir(self.prefix), [".lock"])
        bad = _fake_release(self.root, "edk2-stable202611-r1", tamper=guarded_digest)
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(bad), self.prefix, None, symbols=False)
        self.assertTrue(
            str(cm.exception).endswith("/axl-firmware-edk2-stable202611-r1/guarded/aarch64/vars.fd:"
                                       " not guarded/aarch64's vars as its manifest records it"),
            str(cm.exception),
        )
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


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class DownloadTests(unittest.TestCase):
    """Fetching a release's assets by tag."""

    def setUp(self) -> None:
        self.seen: list[urllib.request.Request] = []
        self.dest = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dest)

    def _urlopen(self, req: urllib.request.Request, timeout: float = 0) -> _FakeResponse:
        self.seen.append(req)
        url = req.full_url
        if url.endswith("/releases/tags/t1"):
            return _FakeResponse(json.dumps({"assets": [{"name": "a.tar.xz", "url": "https://api.github.com/x/1"}]}).encode())
        if url.endswith("/a.tar.xz") or url == "https://api.github.com/x/1":
            return _FakeResponse(b"asset bytes")
        raise urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)

    def _download(self, env: dict[str, str]) -> None:
        with (
            mock.patch.object(install.shutil, "which", return_value=None),
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(install.urllib.request, "urlopen", side_effect=self._urlopen),
        ):
            if "GITHUB_TOKEN" not in env:
                os.environ.pop("GITHUB_TOKEN", None)
            install.download("o/r", "t1", ["a.tar.xz"], self.dest)

    def test_public_release_without_gh_or_token(self) -> None:
        self._download({})
        self.assertEqual((self.dest / "a.tar.xz").read_bytes(), b"asset bytes")
        self.assertEqual(
            [(r.full_url, r.get_header("Authorization")) for r in self.seen],
            [("https://github.com/o/r/releases/download/t1/a.tar.xz", None)],
        )

    def test_public_release_missing_asset(self) -> None:
        with self.assertRaises(install.InstallError) as e:
            with (
                mock.patch.object(install.shutil, "which", return_value=None),
                mock.patch.object(install.urllib.request, "urlopen", side_effect=self._urlopen),
            ):
                os.environ.pop("GITHUB_TOKEN", None)
                install.download("o/r", "t1", ["missing.tar.xz"], self.dest)
        self.assertEqual(
            str(e.exception),
            "cannot fetch o/r t1 missing.tar.xz: HTTP 404 from "
            "https://github.com/o/r/releases/download/t1/missing.tar.xz "
            "(a private repository needs gh, logged in, or GITHUB_TOKEN)",
        )

    def test_gh_not_logged_in_falls_back_to_the_public_url(self) -> None:
        failed = subprocess.CompletedProcess[str](args=[], returncode=4, stdout="", stderr="gh auth login")
        with (
            mock.patch.object(install.shutil, "which", return_value="/usr/bin/gh"),
            mock.patch.object(install.subprocess, "run", return_value=failed),
            mock.patch.object(install.urllib.request, "urlopen", side_effect=self._urlopen),
        ):
            os.environ.pop("GITHUB_TOKEN", None)
            install.download("o/r", "t1", ["a.tar.xz"], self.dest)
        self.assertEqual((self.dest / "a.tar.xz").read_bytes(), b"asset bytes")
        self.assertEqual(
            [r.full_url for r in self.seen], ["https://github.com/o/r/releases/download/t1/a.tar.xz"]
        )

    def test_token_uses_the_api(self) -> None:
        self._download({"GITHUB_TOKEN": "tok"})
        self.assertEqual((self.dest / "a.tar.xz").read_bytes(), b"asset bytes")
        self.assertEqual(
            [r.full_url for r in self.seen],
            ["https://api.github.com/repos/o/r/releases/tags/t1", "https://api.github.com/x/1"],
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
        # The install requires the image sets the assembly describes, and
        # checks every other it lists (GUARDED's).
        self.assertEqual(sorted(img.dir for img in release.DESCRIBED), sorted(install.IMAGE_DIRS))
        self.assertEqual(
            [img.dir for img in release.IMAGES if not img.described and install.IMAGE_SET.fullmatch(img.dir)],
            ["guarded/x64", "guarded/aarch64"],
        )
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

        def no_licences(m: Manifest) -> None:
            del cast("dict[str, dict[str, object]]", m["images"])["release/x64"]["licenses"]

        def no_edk2_licence(m: Manifest) -> None:
            cast("dict[str, dict[str, object]]", m["images"])["x64"]["licenses"] = []

        def not_in_top(m: Manifest) -> None:
            m["licenses"] = ["LICENSES/other.txt"]

        def hardware_debug(m: Manifest) -> None:
            m["hardware_images"] = {"x64": "x64", "aarch64": "release/aarch64"}

        def no_hardware(m: Manifest) -> None:
            del m["hardware_images"]

        # r3 console: a required patch not listed as applied.
        def patch_missing(m: Manifest) -> None:
            edk2 = cast("dict[str, list[dict[str, str]]]", m["edk2"])
            edk2["patches"] = [x for x in edk2["patches"] if x["name"] != "0007-shell-ctrl-s.patch"]

        def hardware_guarded(m: Manifest) -> None:
            m["hardware_images"] = {"x64": "release/x64", "aarch64": "guarded/aarch64"}

        def no_guarded(m: Manifest) -> None:
            del cast("dict[str, object]", m["images"])["guarded/x64"]

        def guarded_descriptor(m: Manifest) -> None:
            cast("dict[str, dict[str, object]]", m["images"])["guarded/x64"]["descriptor"] = "descriptors/30-x.json"

        want = sorted(img.dir for img in release.IMAGES)
        cases: dict[str, tuple[Callable[[Manifest], None], str]] = {
            "moved": (moved, "FAIL: x64: the manifest's code is 'release/x64/code.fd', not 'x64/code.fd'\n"),
            "empty": (
                empty,
                f"FAIL: manifest images [], not {want}\n"
                "FAIL: hardware_images.x64: 'release/x64' is not an image of the release\n"
                "FAIL: hardware_images.aarch64: 'release/aarch64' is not an image of the release\n",
            ),
            "no edk2": (no_edk2, "FAIL: {m}: no edk2.tag\n"),
            "no code": (no_code, "FAIL: aarch64: the manifest's code is None, not 'aarch64/code.fd'\n"),
            "no licences": (no_licences, "FAIL: release/x64: licenses is None, not a list\n"),
            "no edk2 licence": (
                no_edk2_licence,
                "FAIL: x64: edk2's licence LICENSES/edk2-License.txt is not listed, chosen\n",
            ),
            "not in top": (
                not_in_top,
                "".join(
                    f"FAIL: {img.dir}: LICENSES/edk2-License.txt is not among the top-level licenses\n"
                    for img in release.IMAGES
                ),
            ),
            "hardware DEBUG": (hardware_debug, "FAIL: hardware_images.x64: 'x64' is of flavour 'DEBUG', not RELEASE\n"),
            "no hardware": (no_hardware, "FAIL: manifest: no hardware_images\n"),
            "patch missing": (
                patch_missing,
                "FAIL: manifest edk2.patches: 0007-shell-ctrl-s.patch: required, but the build did not apply it\n",
            ),
            "hardware GUARDED": (
                hardware_guarded,
                "FAIL: hardware_images.aarch64: 'guarded/aarch64' is of flavour 'GUARDED', not RELEASE\n",
            ),
            "no guarded": (no_guarded, f"FAIL: manifest images {[d for d in want if d != 'guarded/x64']}, not {want}\n"),
            "guarded descriptor": (
                guarded_descriptor,
                "FAIL: guarded/x64: the manifest names a descriptor, 'descriptors/30-x.json'; it has none\n",
            ),
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


class HardwareImagesTests(unittest.TestCase):
    """hardware_images: per arch, the RELEASE image a consumer ships to real hardware."""

    IMAGES: Manifest = {
        img.dir: {"arch": img.arch, "flavour": img.flavour, "code": {}} for img in release.IMAGES
    }

    def test_the_release_names_release_images(self) -> None:
        self.assertEqual(release.HARDWARE_IMAGES, {"x64": "release/x64", "aarch64": "release/aarch64"})
        self.assertEqual(release.check_hardware_images(release.HARDWARE_IMAGES, self.IMAGES), [])

    def test_refused(self) -> None:
        cases: dict[str, tuple[object, list[str]]] = {
            "a DEBUG image": (
                {"x64": "x64", "aarch64": "release/aarch64"},
                ["hardware_images.x64: 'x64' is of flavour 'DEBUG', not RELEASE"],
            ),
            "both DEBUG": (
                {"x64": "x64", "aarch64": "aarch64"},
                [
                    "hardware_images.x64: 'x64' is of flavour 'DEBUG', not RELEASE",
                    "hardware_images.aarch64: 'aarch64' is of flavour 'DEBUG', not RELEASE",
                ],
            ),
            "another arch's": (
                {"x64": "release/aarch64", "aarch64": "release/aarch64"},
                ["hardware_images.x64: 'release/aarch64' is an image of arch 'aarch64'"],
            ),
            "a missing image": (
                {"x64": "release/x86", "aarch64": "release/aarch64"},
                ["hardware_images.x64: 'release/x86' is not an image of the release"],
            ),
            "not a string": (
                {"x64": ["release/x64"], "aarch64": "release/aarch64"},
                ["hardware_images.x64: ['release/x64'] is not an image of the release"],
            ),
            "an arch left out": (
                {"x64": "release/x64"},
                ["hardware_images: arches ['x64'], not ['aarch64', 'x64']"],
            ),
            "an arch too many": (
                {"x64": "release/x64", "aarch64": "release/aarch64", "riscv64": "release/x64"},
                [
                    "hardware_images: arches ['aarch64', 'riscv64', 'x64'], not ['aarch64', 'x64']",
                    "hardware_images.riscv64: 'release/x64' is an image of arch 'x64'",
                ],
            ),
            "not an object": (["release/x64"], ["hardware_images: ['release/x64'] is not an object"]),
            "absent": (None, ["manifest: no hardware_images"]),
        }
        for label, (value, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(release.check_hardware_images(value, self.IMAGES), want)

    def test_an_entry_not_an_object(self) -> None:
        images: Manifest = {**self.IMAGES, "release/x64": "x"}
        self.assertEqual(
            release.check_hardware_images(release.HARDWARE_IMAGES, images),
            ["hardware_images.x64: 'release/x64' is not an image of the release"],
        )

    def test_an_entry_without_arch_or_flavour(self) -> None:
        images: Manifest = {**self.IMAGES, "release/x64": {"arch": "x64"}, "release/aarch64": {}}
        self.assertEqual(
            release.check_hardware_images(release.HARDWARE_IMAGES, images),
            [
                "hardware_images.x64: 'release/x64' is of flavour None, not RELEASE",
                "hardware_images.aarch64: 'release/aarch64' is an image of arch None",
            ],
        )

    def test_assemble_stops_on_a_hardware_image_problem(self) -> None:
        # Checked with the static checks, before any boot.
        a = release.AssembleArgs(
            ws=Path("/nonexistent/ws"), src=Path("/nonexistent/src"), dist=Path("/nonexistent/dist"),
            stage=Path(tempfile.mkdtemp()) / "stage", release="r", tag="t", commit="c", url="u", fixes=[],
            toolchain="GCC", release_date="01/01/2026", compilers={}, defines={}, pcds={}, patches=[],
            repo_commit="none", repo_dirty=0, built_by="local", qemu_dir=None,
        )  # fmt: skip
        self.addCleanup(shutil.rmtree, a.stage.parent)
        err = io.StringIO()
        with (
            mock.patch.object(release, "HARDWARE_IMAGES", {"x64": "x64", "aarch64": "release/aarch64"}),
            mock.patch.object(release, "stage_images", return_value=[]),
            mock.patch.object(release, "static_checks", return_value=({}, [])),
            mock.patch.object(release, "volinfo_checks", return_value=[]),
            mock.patch.object(release, "isa_checks", return_value=({}, [])),
            mock.patch.object(checks, "build_facts", return_value=({}, [])),
            mock.patch.object(checks, "check_patches", return_value=[]),
            mock.patch.object(release, "boot_checks", side_effect=AssertionError("booted")),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(release.assemble(a), 1)
        self.assertEqual(err.getvalue(), "FAIL: hardware_images.x64: 'x64' is of flavour 'DEBUG', not RELEASE\n")


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

    def test_pin_x64_march(self) -> None:
        here = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as t:
            tools_def = Path(t) / "tools_def.txt"
            text = (
                "DEFINE GCC_X64_CC_FLAGS = -m64\n"
                "  DEBUG_GCC_X64_CC_FLAGS        = DEF(GCC_X64_CC_FLAGS) -flto\n"
                "  DEBUG_GCC_X64_DLINK_FLAGS     = -flto -Os\n"
                "RELEASE_GCC_X64_CC_FLAGS\t= DEF(GCC_X64_CC_FLAGS) -flto\n"
                "  NOOPT_GCC_X64_CC_FLAGS        = -O0\n"
                "  DEBUG_GCC_AARCH64_CC_FLAGS    = -flto\n"
                "*_GCC_X64_ASM_FLAGS = -m64\n"
            )

            def pin(*targets: str) -> tuple[int, str, str]:
                r = subprocess.run(
                    ["bash", "-c", '. "$1"; shift; pin_x64_march "$@"', "-", str(here / "build.sh"), str(tools_def),
                     "-march=x86-64", *targets],
                    capture_output=True,
                    text=True,
                    check=False,
                )  # fmt: skip
                return r.returncode, r.stdout, r.stderr

            pinned = (
                text.replace("-flto\n", "-flto -march=x86-64\n", 1)
                .replace("\t= DEF(GCC_X64_CC_FLAGS) -flto\n", "\t= DEF(GCC_X64_CC_FLAGS) -flto -march=x86-64\n")
                .replace("-O0\n", "-O0 -march=x86-64\n")
            )
            tools_def.write_text(text)
            self.assertEqual(pin("DEBUG", "RELEASE"), (0, "", ""))
            self.assertEqual(tools_def.read_text(), pinned)
            self.assertEqual(sorted(p.name for p in Path(t).iterdir()), ["tools_def.txt"])
            # The template's CRLF: the flag before each CR, which edk2's
            # parser (universal newlines) would end the line at.
            tools_def.write_bytes(text.replace("\n", "\r\n").encode())
            self.assertEqual(pin("DEBUG", "RELEASE"), (0, "", ""))
            self.assertEqual(tools_def.read_bytes(), pinned.replace("\n", "\r\n").encode())
            # A flag already after a CR does not count as pinned.
            tools_def.write_bytes(pinned.replace("-flto -march", "-flto\r -march").encode())
            self.assertEqual(
                pin("DEBUG"),
                (1, "", f"build.sh: {tools_def}: 0 DEBUG_GCC_X64_CC_FLAGS lines to pin -march=x86-64 on, not 1\n"),
            )
            # A flavour with no line, and one with two.
            tools_def.write_text(text.replace("RELEASE_GCC_X64_CC_FLAGS", "RELEASE_GCC_X64_XX_FLAGS"))
            self.assertEqual(
                pin("DEBUG", "RELEASE"),
                (1, "", f"build.sh: {tools_def}: 0 RELEASE_GCC_X64_CC_FLAGS lines to pin -march=x86-64 on, not 1\n"),
            )
            tools_def.write_text(text + "  DEBUG_GCC_X64_CC_FLAGS = again\n")
            self.assertEqual(
                pin("DEBUG"),
                (1, "", f"build.sh: {tools_def}: 2 DEBUG_GCC_X64_CC_FLAGS lines to pin -march=x86-64 on, not 1\n"),
            )

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
