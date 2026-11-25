"""The release's static checks: what an image and its build must hold.

Each check returns the problems it found, as sentences; none means it
passed. The boot checks are in boot.py.
"""

from __future__ import annotations

import re
import struct
import uuid
from pathlib import Path
from typing import NamedTuple

from axlfw import fv


class Driver(NamedTuple):
    """An FFS file a check looks for, by the FILE_GUID of its INF."""

    name: str
    guid: str
    source: str


# The FILE_GUIDs of the INFs at the pinned tag (tests/firmware-tests.sh
# compares each with its INF when the edk2 source is at hand). The Shell's
# FILE_GUID is ShellPkg.dec's gUefiShellFileGuid, which its FDF entry uses.
COMMON_DRIVERS = (
    Driver("VirtioFsDxe", "7BD9DDF7-8B83-488E-AEC9-24C78610289C", "OvmfPkg/VirtioFsDxe/VirtioFsDxe.inf"),
    Driver("VirtioGpuDxe", "D6099B94-CD97-4CC5-8714-7F6312701A8A", "OvmfPkg/VirtioGpuDxe/VirtioGpu.inf"),
    Driver("VirtioRngDxe", "58E26F0D-CBAC-4BBA-B70F-18221415665A", "OvmfPkg/VirtioRngDxe/VirtioRng.inf"),
    Driver("VirtioNetDxe", "A92CDB4B-82F1-4E0B-A516-8A655D371524", "OvmfPkg/VirtioNetDxe/VirtioNet.inf"),
    Driver("VirtioBlkDxe", "11D92DFB-3CA9-4F93-BA2E-4780ED3E03B5", "OvmfPkg/VirtioBlkDxe/VirtioBlk.inf"),
    Driver("VirtioScsiDxe", "FAB5D4F4-83C0-4AAF-8480-442D11DF6CEA", "OvmfPkg/VirtioScsiDxe/VirtioScsi.inf"),
    Driver("VirtioSerialDxe", "23CACE14-EBA4-49F6-9681-C697FF0B649E", "OvmfPkg/VirtioSerialDxe/VirtioSerial.inf"),
    Driver("Ip6Dxe", "5BEDB5CC-D830-4EB2-8742-2D4CC9B54F2C", "NetworkPkg/Ip6Dxe/Ip6Dxe.inf"),
    Driver("HttpDxe", "2366C20F-E15A-11E3-8BF1-E4115B28BC50", "NetworkPkg/HttpDxe/HttpDxe.inf"),
    Driver("TlsDxe", "3ACEB0C0-3C72-11E4-9A56-74D435052646", "NetworkPkg/TlsDxe/TlsDxe.inf"),
    Driver("IScsiDxe", "86CDDF93-4872-4597-8AF9-A35AE4D3725F", "NetworkPkg/IScsiDxe/IScsiDxe.inf"),
    Driver("MnpDxe", "025BBFC7-E6A9-4B8B-82AD-6815A1AEAF4A", "NetworkPkg/MnpDxe/MnpDxe.inf"),
    Driver("Ip4Dxe", "9FB1A1F3-3B71-4324-B39A-745CBB015FFF", "NetworkPkg/Ip4Dxe/Ip4Dxe.inf"),
    Driver("TcpDxe", "1A7E4468-2F55-4A56-903C-01265EB7622B", "NetworkPkg/TcpDxe/TcpDxe.inf"),
    Driver("Dhcp4Dxe", "94734718-0BBC-47FB-96A5-EE7A5AE6A2AD", "NetworkPkg/Dhcp4Dxe/Dhcp4Dxe.inf"),
    Driver("Tcg2Dxe", "FDFF263D-5F68-4591-87BA-B768F445A9AF", "SecurityPkg/Tcg/Tcg2Dxe/Tcg2Dxe.inf"),
    Driver("UsbMouseDxe", "2D2E62AA-9ECF-43B7-8219-94E7FC713DFE", "MdeModulePkg/Bus/Usb/UsbMouseDxe/UsbMouseDxe.inf"),
    Driver(
        "UsbMouseAbsolutePointerDxe",
        "4EA43463-747C-46EB-97FB-B0E5C5F05306",
        "MdeModulePkg/Bus/Usb/UsbMouseAbsolutePointerDxe/UsbMouseAbsolutePointerDxe.inf",
    ),
    Driver("QemuVideoDxe", "E3752948-B9A1-4770-90C4-DF41C38986BE", "OvmfPkg/QemuVideoDxe/QemuVideoDxe.inf"),
    Driver("UiApp", "462CAA21-7614-4503-836E-8AB6F4662331", "MdeModulePkg/Application/UiApp/UiApp.inf"),
    Driver("Shell", "7C04A583-9E3E-4F1C-AD65-E05268D0B4D1", "ShellPkg/ShellPkg.dec:gUefiShellFileGuid"),
)
# X64 only: the PC serial port; the PS/2 mouse; SnpDxe, which ArmVirtQemu
# does not build (its NICs are virtio-net, whose VirtioNetDxe produces SNP
# itself; SnpDxe serves UNDI option ROMs).
X64_DRIVERS = (
    Driver("SnpDxe", "A2F436EA-A127-4EF8-957C-8048606FF670", "NetworkPkg/SnpDxe/SnpDxe.inf"),
    Driver(
        "PciSioSerialDxe",
        "E2775B47-D453-4EE3-ADA7-391A1B05AC17",
        "MdeModulePkg/Bus/Pci/PciSioSerialDxe/PciSioSerialDxe.inf",
    ),
    Driver("Ps2MouseDxe", "08464531-4C99-4C4C-A887-8D8BA4BBB063", "MdeModulePkg/Bus/Isa/Ps2MouseDxe/Ps2MouseDxe.inf"),
)
# Files no image may hold: Secure Boot's configuration driver, built only
# with SECURE_BOOT_ENABLE.
ABSENT = (
    Driver(
        "SecureBootConfigDxe",
        "F0E6A44F-7195-41C3-AC64-54F202CD0A21",
        "SecurityPkg/VariableAuthenticated/SecureBootConfigDxe/SecureBootConfigDxe.inf",
    ),
)
ARCHES = ("x64", "aarch64")
FLAVOURS = ("DEBUG", "RELEASE")
UIAPP = "462CAA21-7614-4503-836E-8AB6F4662331"
# PE machine types.
_MACHINE = {"x64": 0x8664, "aarch64": 0xAA64}
_EFI_APPLICATION = 10
# PcdDebugPropertyMask bits (MdePkg's DebugLib.h).
ASSERT_ENABLED = 0x01
CLEAR_MEMORY = 0x08
ASSERT_BREAKPOINT = 0x10
ASSERT_DEADLOOP = 0x20
# PcdDebugPrintErrorLevel per arch, as QEMU's bundled images: OvmfPkgX64's
# default, and [opts.armvirt.silent]'s errors only.
PRINT_LEVEL = {"x64": 0x8000004F, "aarch64": 0x80000000}


def drivers(arch: str) -> tuple[Driver, ...]:
    """The FFS files every image of arch must hold."""
    return COMMON_DRIVERS + (X64_DRIVERS if arch == "x64" else ())


def check_image(arch: str, image: Path) -> list[str]:
    """Every required driver is in the image, and no absent one is.

    An absent file proves nothing unless the walk reached the volume it
    would be in: the required drivers sit in the same (compressed) DXE
    volume, so their presence is that proof.
    """
    try:
        guids = fv.image_guids(image)
    except (OSError, fv.NotAnImageError) as err:
        return [f"{image}: {err}"]
    problems: list[str] = []
    missing = [d for d in drivers(arch) if d.guid not in guids]
    if missing:
        problems.append(f"{image} lacks: " + ", ".join(f"{d.name} ({d.guid})" for d in missing))
    present = [d for d in ABSENT if d.guid in guids]
    if present:
        problems.append(f"{image} holds: " + ", ".join(f"{d.name} ({d.guid})" for d in present))
    return problems


def check_shell(arch: str, path: Path) -> list[str]:
    """The file is a UEFI application (PE32+) for arch."""
    try:
        data = path.read_bytes()
    except OSError as err:
        return [f"{path}: {err.strerror}"]
    if data[:2] != b"MZ" or len(data) < 0x40:
        return [f"{path}: not a PE image"]
    (pe,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe : pe + 4] != b"PE\0\0":
        return [f"{path}: not a PE image"]
    # The signature, the file header (20 bytes), and the optional header up
    # to its Subsystem field (offset 68, two bytes).
    if len(data) < pe + 24 + 70:
        return [f"{path}: a truncated PE header ({len(data)} bytes)"]
    (machine,) = struct.unpack_from("<H", data, pe + 4)
    (magic,) = struct.unpack_from("<H", data, pe + 24)
    (subsystem,) = struct.unpack_from("<H", data, pe + 24 + 68)
    problems: list[str] = []
    if machine != _MACHINE[arch]:
        problems.append(f"{path}: machine {machine:#06x}, not {arch}'s {_MACHINE[arch]:#06x}")
    if magic != 0x20B:
        problems.append(f"{path}: not PE32+ (magic {magic:#x})")
    if subsystem != _EFI_APPLICATION:
        problems.append(f"{path}: subsystem {subsystem}, not an EFI application ({_EFI_APPLICATION})")
    return problems


_PCD_VALUE = re.compile(r"^#define _PCD_VALUE_(\w+)\s+(\S+?)U?\s*$", re.MULTILINE)
_PCD_ARRAY = re.compile(r"_gPcd_FixedAtBuild_(\w+)\[\d+\] = \{([^}]*)\}")


def module_pcds(module: Path) -> dict[str, str]:
    """The FixedAtBuild PCD values in a built module's AutoGen files."""
    values: dict[str, str] = {}
    for name in ("AutoGen.h", "AutoGen.c"):
        path = module / name
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        values.update(_PCD_VALUE.findall(text))
        for pcd, body in _PCD_ARRAY.findall(text):
            values[pcd] = body.replace(" ", "")
    return values


def _module_dir(build: Path, arch: str, rel: str) -> Path:
    return build / ("X64" if arch == "x64" else "AARCH64") / rel / "DEBUG"


def _guid_bytes(guid: str) -> str:
    return ",".join(f"0x{b:02x}" for b in uuid.UUID(guid).bytes_le)


def build_facts(build: Path, arch: str, flavour: str) -> tuple[dict[str, object], list[str]]:
    """What a build's AutoGen says of its debug flavour and front page.

    build is the platform's <TARGET>_<TOOLCHAIN> directory. Returns the
    facts for the manifest and the problems found.
    """
    problems: list[str] = []
    dxe = _module_dir(build, arch, "MdeModulePkg/Core/Dxe/DxeMain")
    bds = _module_dir(build, arch, "MdeModulePkg/Universal/BdsDxe/BdsDxe")
    pcds = module_pcds(dxe)
    menu = module_pcds(bds).get("PcdBootManagerMenuFile", "")
    # A RELEASE build may link a DebugLib that references neither PCD
    # (ArmVirtQemu's does): then there is nothing to read, and none needed.
    if flavour == "DEBUG" and ("PcdDebugPropertyMask" not in pcds or "PcdDebugPrintErrorLevel" not in pcds):
        return {}, [f"{dxe}: no AutoGen with the debug PCDs"]
    mask = int(pcds["PcdDebugPropertyMask"], 0) if "PcdDebugPropertyMask" in pcds else None
    level = int(pcds["PcdDebugPrintErrorLevel"], 0) if "PcdDebugPrintErrorLevel" in pcds else None
    # The module's makefile: GNUmakefile with GCC (BaseTools' GenMake).
    makefile = dxe.parent / "GNUmakefile"
    if not makefile.is_file():
        return {}, [f"{makefile}: missing, so the build's flags cannot be read"]
    ndebug = "MDEPKG_NDEBUG" in makefile.read_text(errors="replace")
    facts: dict[str, object] = {
        "target": flavour,
        "asserts_compiled": not ndebug,
        "property_mask": None if mask is None else f"{mask:#04x}",
        "print_error_level": None if level is None else f"{level:#010x}",
        "boot_manager_menu": "UiApp" if menu == _guid_bytes(UIAPP) else menu,
    }
    if menu != _guid_bytes(UIAPP):
        problems.append(f"{bds}: PcdBootManagerMenuFile is {menu or 'unset'}, not UiApp's {UIAPP}")
    if not build.name.startswith(f"{flavour}_"):
        problems.append(f"{build}: not a {flavour} build")
    if flavour == "RELEASE":
        if not ndebug:
            problems.append(f"{makefile}: a RELEASE build without MDEPKG_NDEBUG")
        return facts, problems
    if ndebug:
        problems.append(f"{makefile}: a DEBUG build with MDEPKG_NDEBUG")
    assert mask is not None and level is not None  # DEBUG: required above
    if level != PRINT_LEVEL[arch]:
        problems.append(f"{dxe}: PcdDebugPrintErrorLevel {level:#010x}, not {PRINT_LEVEL[arch]:#010x}")
    if arch == "x64":
        facts["assert"] = (
            "deadloop" if mask & ASSERT_DEADLOOP else "breakpoint" if mask & ASSERT_BREAKPOINT else "none"
        )
        facts["clear_memory"] = bool(mask & CLEAR_MEMORY)
        if not mask & ASSERT_ENABLED or not mask & (ASSERT_DEADLOOP | ASSERT_BREAKPOINT) or not mask & CLEAR_MEMORY:
            problems.append(
                f"{dxe}: PcdDebugPropertyMask {mask:#04x} lacks ASSERT (0x01 with 0x10 or 0x20)"
                " or CLEAR_MEMORY (0x08)"
            )
    return facts, problems


def source_guids(src: Path) -> list[str]:
    """Each checked GUID compared with the INF (or DEC) it was taken from."""
    problems: list[str] = []
    for d in COMMON_DRIVERS + X64_DRIVERS + ABSENT:
        path, _, symbol = d.source.partition(":")
        text = (src / path).read_text(errors="replace")
        if symbol:
            m = re.search(rf"{symbol}\s*=\s*\{{([^}}]*)\}}", text)
            nums = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]+)", m.group(1))] if m else []
            got = (
                f"{nums[0]:08X}-{nums[1]:04X}-{nums[2]:04X}-{nums[3]:02X}{nums[4]:02X}-"
                + "".join(f"{n:02X}" for n in nums[5:11])
                if len(nums) == 11
                else "none"
            )
        else:
            m = re.search(r"^\s*FILE_GUID\s*=\s*(\S+)", text, re.MULTILINE)
            got = m.group(1).upper() if m else "none"
        if got != d.guid:
            problems.append(f"{d.name}: {d.source} says {got}, the check {d.guid}")
    return problems
