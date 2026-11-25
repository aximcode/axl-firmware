"""Name the FFS files in a UEFI firmware image.

The parsing is uefi_firmware's (pinned in requirements.txt): it decodes the
volumes, their nested volume images and the LZMA GUID-defined sections that
OVMF and ArmVirtQemu compress their DXE volumes into. Its own image
detection (``AutoParser``) misses ArmVirtQemu's image, whose first volume
does not start at offset 0, so the volumes are found here by their
``_FVH`` signature and each is handed to it.

Checked against axl-sdk's ``fwtool list`` on edk2-stable202602's OVMF and
ArmVirtQemu, QEMU 10.0.0's bundled images and AlmaLinux's: the same file
GUIDs on every one (docs/DESIGN.md §4.15 and §6). Every release image is
read again by edk2's VolInfo, and the two sets must agree (volinfo.py,
§4.23).
"""

from __future__ import annotations

import contextlib
import io
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uefi_firmware.uefi import FirmwareObject, FirmwareVolume

# EFI_FIRMWARE_VOLUME_HEADER: the signature's offset in the header.
_SIGNATURE = b"_FVH"
_SIGNATURE_OFFSET = 40
# The free space at a volume's end parses as a file with this GUID, and
# pad files (EFI_FV_FILETYPE_FFS_PAD) are named by it too.
_FREE_SPACE = "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"


class NotAnImageError(Exception):
    """The file holds no firmware volume, or one that does not parse."""


def _guid(raw: bytes) -> str:
    return str(uuid.UUID(bytes_le=raw)).upper()


def _volumes(data: bytes) -> Iterator[FirmwareVolume]:
    from uefi_firmware.uefi import FirmwareVolume  # noqa: PLC0415 - optional dependency

    view = memoryview(data)
    pos = 0
    while (i := data.find(_SIGNATURE, pos)) >= 0:
        start = i - _SIGNATURE_OFFSET
        if start >= 0:
            volume = FirmwareVolume(view[start:])
            if volume.valid_header:
                if not volume.process():
                    msg = f"the volume at {start:#x} does not parse"
                    raise NotAnImageError(msg)
                yield volume
                pos = start + volume.size
                continue
        pos = i + len(_SIGNATURE)


def file_guids(data: bytes) -> set[str]:
    """The GUIDs (upper case) of every FFS file in the image's volumes.

    Raises NotAnImageError when there is no volume, or one fails to parse.
    """
    from uefi_firmware.uefi import FirmwareFile  # noqa: PLC0415

    found: set[str] = set()
    volumes = 0
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        for volume in _volumes(data):
            volumes += 1
            stack: list[FirmwareObject | None] = [volume]
            while stack:
                node = stack.pop()
                if node is None:
                    continue
                if isinstance(node, FirmwareFile):
                    found.add(_guid(node.guid))
                stack.extend(node.objects)
    if volumes == 0:
        msg = "no firmware volume found"
        raise NotAnImageError(msg)
    found.discard(_FREE_SPACE)
    return found


def image_guids(path: Path) -> set[str]:
    """file_guids of the image at path."""
    return file_guids(path.read_bytes())
