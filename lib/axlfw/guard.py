"""The guarded image's boot check: a write one byte past a pool buffer.

tests/guard-overrun/ holds a UEFI application, Overrun.efi, that allocates
a 64-byte pool buffer of EfiBootServicesData and writes the byte after it
(build.sh builds it for each arch, out of tree). Run from the Shell of each
DEBUG and GUARDED image set:

- GUARDED (the UEFI pool guard, the pool against its tail guard page,
  checks.HEAP_GUARD) must fault on that write: X64's page fault, AARCH64's
  synchronous exception, before the application's second line;
- DEBUG must not notice it: the application prints its second line and the
  Shell's prompt comes back. This is the control: the write is made, and
  only the guard catches it.

docs/DESIGN.md §4.34.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from axlfw import boot

APP = "Overrun.efi"
# Overrun.c's two lines: before the write, and after it.
BEFORE = "axlfw-overrun: writing byte 64 of a 64-byte pool buffer"
AFTER = "axlfw-overrun: written, no fault"
# What the CPU exception handler prints on the console (CpuExceptionHandlerLib,
# through the serial port), each placing the fault in Overrun.efi: X64's
# page fault on a write to a page not present, then the image it found at
# the faulting RIP; AARCH64's synchronous exception (a data abort on the
# guard page), then the first frame of its backtrace.
FAULT = {
    "x64": re.compile(
        r"!!!! X64 Exception Type - 0E\(#PF - Page-Fault\).*\n.*W:1 P:0(?:.*\n)+?"
        r"!!!! Find image based on IP\(0x[0-9A-Fa-f]+\) \S*Overrun\.dll"
    ),
    "aarch64": re.compile(
        r"Synchronous Exception at 0x[0-9A-Fa-f]+\n"
        r"PC 0x[0-9A-Fa-f]+ \(0x[0-9A-Fa-f]+\+0x[0-9A-Fa-f]+\) \[ *\d+\] Overrun\.dll"
    ),
}
# How long the write has, from the first line, to fault or print the second:
# both came within a second (local, AlmaLinux 10 x86_64, QEMU 10.0.0; X64
# under KVM, AARCH64 under TCG).
BOUND = 30.0
FLAVOURS = ("DEBUG", "GUARDED")


def app(ws: Path, toolchain: str, arch: str) -> Path:
    """Where build.sh's build of Overrun.efi for arch is."""
    return ws / "Build" / "AxlFwGuard" / f"DEBUG_{toolchain}" / arch.upper() / APP


def outcome(g: boot.Guest, arch: str, bound: float = BOUND) -> str:
    """Run Overrun.efi at the Shell's prompt: "fault" or "silent".

    BootError when it does not start, or neither faults nor finishes within
    bound, or finishes and the prompt does not come back.
    """
    g.send(f"fs0:\\{APP}\r")
    g.expect(re.compile(re.escape(BEFORE)), f"{APP}'s first line")
    m = g.expect(re.compile(f"{re.escape(AFTER)}|{FAULT[arch].pattern}"), f"{APP}'s fault or second line", bound)
    if m.group(0) != AFTER:
        return "fault"
    g.expect(boot.PROMPT, f"the Shell's prompt after {APP}")
    return "silent"


def check(s: boot.ImageSet, overrun: Path, qemu_dir: Path | None, workdir: Path, timeout: float) -> boot.Result:
    """Boot s, run Overrun.efi: GUARDED must fault, DEBUG stay silent.

    The result's facts: {"pool_overrun": "fault" | "silent"}, when it ran.
    """
    result = boot.Result()
    want = "fault" if s.flavour == "GUARDED" else "silent"
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workdir) as tmp, tempfile.TemporaryDirectory(prefix="axlfw-") as socks:
        t = Path(tmp)
        esp = t / "esp"
        (esp / "EFI" / "BOOT").mkdir(parents=True)
        shutil.copyfile(s.shell, esp / "EFI" / "BOOT" / boot.BOOT_FILE[s.arch])
        shutil.copyfile(overrun, esp / APP)
        vars_copy = t / "vars.fd"
        shutil.copyfile(s.vars, vars_copy)
        name = f"{s.arch}-{s.flavour.lower()}-overrun"
        try:
            g = boot.Guest(boot.qemu_argv(qemu_dir, s, vars_copy, esp), workdir, Path(socks), name, timeout)
        except boot.BootError as err:
            result.problems.append(f"{s.arch} {s.flavour}: {err}")
            return result
        try:
            g.prompt()
            got = outcome(g, s.arch)
        except boot.BootError as err:
            result.problems.append(f"{s.arch} {s.flavour}: {err}")
        else:
            result.facts["pool_overrun"] = got
            if got != want:
                said = {
                    "fault": "faulted on a write one byte past a pool buffer",
                    "silent": "did not fault on a write one byte past a pool buffer",
                }
                result.problems.append(f"{s.arch} {s.flavour}: {APP} {said[got]}; {want} was expected")
        finally:
            g.close()
    return result
