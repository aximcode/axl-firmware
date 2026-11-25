# The part of uefi_firmware 1.17's uefi module that lib/axlfw/fv.py uses.
from __future__ import annotations

from typing import Sequence

class FirmwareObject:
    @property
    def objects(self) -> Sequence[FirmwareObject | None]: ...

class FirmwareFile(FirmwareObject):
    guid: bytes

class FirmwareVolume(FirmwareObject):
    valid_header: bool
    size: int
    def __init__(self, data: bytes | memoryview, name: str = ...) -> None: ...
    def process(self) -> bool: ...
