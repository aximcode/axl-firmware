"""The release's boot checks: one image set booted in QEMU, its Shell driven
over the serial console.

Per arch and flavour, four boots, each from a FAT disk whose removable
path holds the release's own Shell:

- serial only: the Shell's `mode` list; a variable written (`setvar -nv`),
  a warm reset, the variable read back (`dmpstore`); SMBIOS type 0's BIOS
  version (`smbiosview -t 0`); X64's debugcon output; then `reset -s`;
- std-VGA, a new QEMU on the same vars file: the variable read back after
  a cold start; the `mode` list; the GOP's resolution (a screendump); then
  the Shell's `exit`, which must reach the front page;
- virtio-gpu: the `mode` list;
- bochs-display: the `mode` list, and the GOP QemuVideoDxe gives it (its
  driver managing a device, and a screendump at the GOP's resolution).

X64 runs under KVM when /dev/kvm is usable, else TCG; AARCH64 under TCG.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

NV_GUID = "7f1e3a52-6b0d-4c8e-9a27-5d4b1c3e2f60"
NV_NAME = "AxlFirmwareNvCheck"
GOP_DEFAULT = (1280, 800)
SHAPES = ("serial", "std-vga", "virtio-gpu", "bochs-display")
# AARCH64's CPU for the boots: axlvmpool's default. Each of AARCH64_CPUS must
# reach the Shell too: `max` has FEAT_LPA2, on which edk2 202511 to 202602
# hung after its banner (edk2 issue #11962, fixed by b8df7d9c).
AARCH64_CPU = "cortex-a57"
AARCH64_CPUS = ("max", "cortex-a57", "neoverse-v1")
_DEVICE = {"std-vga": "VGA", "virtio-gpu": "virtio-gpu-pci", "bochs-display": "bochs-display"}
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[0-9A-Za-z=>]")
_PROMPT = re.compile(r"(?:Shell|FS\d+:[^>\n]*)> ")
_PPM = re.compile(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s(.*)\Z", re.DOTALL)
_MODE = re.compile(r"Col\s+(\d+)\s+Row\s+(\d+)\s+(\*?)")


class BootError(Exception):
    """A boot did not do what the check needed: said with its transcript."""


class Guest:
    """One QEMU process, its serial console on a pipe, its monitor on a socket."""

    def __init__(self, argv: list[str], workdir: Path, sockdir: Path, name: str, timeout: float) -> None:
        """Start QEMU; its console transcript goes to <workdir>/<name>.log.

        sockdir holds the monitor's socket: a short path, since a socket's
        path is limited to 108 bytes.
        """
        self.name = name
        self.timeout = timeout
        self.monitor_path = sockdir / f"{name}.mon"
        self.stderr_path = workdir / f"{name}.stderr"
        self.log_path = workdir / f"{name}.log"
        self._raw = bytearray()
        self._text = ""
        self._mark = 0
        self._cond = threading.Condition()
        full = [
            *argv,
            "-monitor",
            f"unix:{self.monitor_path},server=on,wait=off",
        ]
        self._stderr = self.stderr_path.open("wb")
        self.proc = subprocess.Popen(  # noqa: S603 - argv is ours
            full, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        out = self.proc.stdout
        assert out is not None
        with self.log_path.open("wb") as log:
            while chunk := os.read(out.fileno(), 65536):
                log.write(chunk)
                log.flush()
                with self._cond:
                    self._raw += chunk
                    self._text = _ANSI.sub(b"", bytes(self._raw)).replace(b"\r", b"").decode("latin-1")
                    self._cond.notify_all()
        with self._cond:
            self._cond.notify_all()

    def expect(self, pattern: re.Pattern[str], what: str, timeout: float | None = None) -> re.Match[str]:
        """Wait for pattern in the console after the mark; move the mark past it."""
        deadline = time.monotonic() + (timeout or self.timeout)
        with self._cond:
            while True:
                m = pattern.search(self._text, self._mark)
                if m:
                    self._mark = m.end()
                    return m
                left = deadline - time.monotonic()
                if left <= 0 or (self.proc.poll() is not None and not self._reader.is_alive()):
                    tail = self._text[-600:]
                    if self.proc.poll() is not None:
                        err = self.stderr_path.read_text(errors="replace").strip()
                        tail += f"\nQEMU exited {self.proc.returncode}: {err}"
                    msg = f"{self.name}: no {what} within {timeout or self.timeout:.0f} s; console ends:\n{tail}"
                    raise BootError(msg)
                self._cond.wait(min(left, 0.5))

    def send(self, text: str) -> None:
        """Type text on the console."""
        stdin = self.proc.stdin
        assert stdin is not None
        for ch in text.encode():
            stdin.write(bytes([ch]))
            stdin.flush()
            time.sleep(0.005)

    def prompt(self) -> None:
        """Wait for the Shell's prompt, skipping startup.nsh's countdown."""
        m = self.expect(re.compile(r"Press ESC in|" + _PROMPT.pattern), "Shell prompt")
        if m.group(0).startswith("Press"):
            self.send("\x1b")
            self.expect(_PROMPT, "Shell prompt")

    def command(self, line: str) -> str:
        """Run one Shell command; its output, up to the next prompt."""
        self.send(line + "\r")
        start = self._mark
        m = self.expect(_PROMPT, f"prompt after `{line}`")
        with self._cond:
            out = self._text[start : m.start()]
        # Drop the command's own echo.
        return out.split("\n", 1)[1] if "\n" in out else ""

    def monitor(self, line: str) -> str:
        """One HMP command; its reply. A QEMU gone is a BootError."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(30)
                s.connect(str(self.monitor_path))
                reply = b""
                while b"(qemu) " not in reply:
                    chunk = s.recv(4096)
                    if not chunk:
                        msg = f"{self.name}: the monitor closed before its prompt"
                        raise BootError(msg)
                    reply += chunk
                s.sendall(line.encode() + b"\n")
                reply = b""
                while b"(qemu) " not in reply:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    reply += chunk
        except OSError as err:
            msg = f"{self.name}: monitor `{line}`: {err}"
            raise BootError(msg) from err
        return reply.decode("latin-1")

    def screen(self, path: Path) -> tuple[int, int, bool]:
        """The display's size, and whether any pixel is lit, from a screendump.

        A display QEMU blanks (a VGA whose attribute controller was never
        unblanked) dumps all black at its full size: the size alone does not
        show that anything is drawn.
        """
        self.monitor(f"screendump {path}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            got = parse_ppm(path.read_bytes()) if path.is_file() else None
            if got:
                return got
            time.sleep(0.2)
        msg = f"{self.name}: no complete screendump at {path}"
        raise BootError(msg)

    def wait_exit(self, timeout: float) -> int:
        """Wait for QEMU to exit by itself."""
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            msg = f"{self.name}: QEMU did not exit within {timeout:.0f} s"
            raise BootError(msg) from None

    def close(self) -> None:
        """Stop QEMU if it is still running."""
        if self.proc.poll() is None:
            try:
                self.monitor("quit")
                self.proc.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait()
        self._reader.join(10)
        self._stderr.close()


@dataclass
class ImageSet:
    """One arch and flavour of a release, as unpacked."""

    arch: str
    flavour: str
    code: Path
    vars: Path
    shell: Path
    edk2_tag: str


@dataclass
class Result:
    """What the boots measured, and the problems they found."""

    facts: dict[str, object] = field(default_factory=dict[str, object])
    problems: list[str] = field(default_factory=list[str])


def _kvm() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def qemu_argv(qemu_dir: Path | None, s: ImageSet, vars_copy: Path, esp: Path, cpu: str = AARCH64_CPU) -> list[str]:
    """The QEMU command line shared by every boot of s (cpu: AARCH64's)."""
    binary = "qemu-system-x86_64" if s.arch == "x64" else "qemu-system-aarch64"
    exe = str(qemu_dir / binary) if qemu_dir else (shutil.which(binary) or binary)
    if s.arch == "x64":
        machine = ["-machine", "q35", *(["-accel", "kvm", "-cpu", "host"] if _kvm() else ["-accel", "tcg", "-cpu", "max"])]
    else:
        machine = ["-machine", "virt", "-accel", "tcg", "-cpu", cpu]
    return [
        exe,
        *machine,
        "-m",
        "1024",
        "-smp",
        "2",
        "-nodefaults",
        "-no-user-config",
        "-display",
        "none",
        "-chardev",
        "stdio,id=con,signal=off",
        "-serial",
        "chardev:con",
        "-drive",
        f"if=pflash,format=raw,unit=0,readonly=on,file={s.code}",
        "-drive",
        f"if=pflash,format=raw,unit=1,file={vars_copy}",
        "-drive",
        f"if=none,id=esp,format=raw,readonly=on,file=fat:{esp}",
        "-device",
        "virtio-blk-pci,drive=esp",
    ]


def parse_ppm(data: bytes) -> tuple[int, int, bool] | None:
    """A complete binary PPM's width, height, and whether any pixel is lit."""
    m = _PPM.match(data)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    pixels = m.group(4)
    if len(pixels) < 3 * w * h:
        return None
    return w, h, pixels.strip(b"\0") != b""


def text_modes(out: str) -> dict[str, object]:
    """The `mode` command's list and its current mode."""
    modes = [[int(c), int(r)] for c, r, _ in _MODE.findall(out)]
    current = [[int(c), int(r)] for c, r, star in _MODE.findall(out) if star]
    if not modes or len(current) != 1:
        msg = f"no mode list in:\n{out}"
        raise BootError(msg)
    return {"modes": modes, "current": current[0]}


def hex_bytes(out: str) -> str:
    """The data bytes of a dmpstore dump, as lower-case hex."""
    data = ""
    for line in out.splitlines():
        m = re.match(r"\s*[0-9A-Fa-f]{8}: ([0-9A-Fa-f -]+?)\s*\*", line)
        if m:
            data += re.sub(r"[^0-9A-Fa-f]", "", m.group(1))
    return data.lower()


def run(s: ImageSet, qemu_dir: Path | None, workdir: Path, timeout: float) -> Result:
    """Every boot check of s; the transcripts are kept in workdir."""
    workdir.mkdir(parents=True, exist_ok=True)
    result = Result()
    with tempfile.TemporaryDirectory(dir=workdir) as tmp, tempfile.TemporaryDirectory(prefix="axlfw-") as socks:
        t = Path(tmp)
        esp = t / "esp"
        boot = esp / "EFI" / "BOOT"
        boot.mkdir(parents=True)
        shutil.copyfile(s.shell, boot / ("BOOTX64.EFI" if s.arch == "x64" else "BOOTAA64.EFI"))
        vars_copy = t / "vars.fd"
        shutil.copyfile(s.vars, vars_copy)
        base = qemu_argv(qemu_dir, s, vars_copy, esp)
        tag = f"{s.arch}-{s.flavour.lower()}"
        token = secrets.token_hex(8)
        modes: dict[str, object] = {}
        result.facts["text_modes"] = modes

        def attempt(name: str, extra: list[str], body: Callable[[Guest], None]) -> None:
            g = Guest([*base, *extra], workdir, Path(socks), f"{tag}-{name}", timeout)
            try:
                body(g)
            except BootError as err:
                result.problems.append(f"{s.arch} {s.flavour}: {err}")
            finally:
                g.close()

        debugcon = workdir / f"{tag}-debugcon.log"
        uart1 = workdir / f"{tag}-uart1.log"
        serial_extra = (
            ["-debugcon", f"file:{debugcon}", "-global", "isa-debugcon.iobase=0x402"]
            if s.arch == "x64"
            else ["-serial", f"file:{uart1}"]
        )

        def serial(g: Guest) -> None:
            g.prompt()
            modes["serial"] = text_modes(g.command("mode"))
            out = g.command("smbiosview -t 0")
            m = re.search(r"BIOS Version:\s*(\S+)", out)
            version = m.group(1) if m else None
            result.facts["smbios_bios_version"] = version
            if version != s.edk2_tag:
                result.problems.append(
                    f"{s.arch} {s.flavour}: SMBIOS type 0 BIOS version {version!r}, not {s.edk2_tag!r}"
                )
            g.command(f"setvar {NV_NAME} -guid {NV_GUID} -nv -bs ={token}")
            g.send("reset\r")
            g.prompt()
            got = hex_bytes(g.command(f"dmpstore {NV_NAME} -guid {NV_GUID}"))
            result.facts["nv_after_warm_reset"] = got == token
            if got != token:
                result.problems.append(
                    f"{s.arch} {s.flavour}: after a warm reset the variable reads {got or 'nothing'}, not {token}"
                )
            g.send("reset -s\r")
            g.wait_exit(timeout)

        attempt("serial", serial_extra, serial)
        # The DEBUG flavour, from what reached its debug channel: X64's
        # verbose output (INFO lines, "Loading driver at ...") on the 0x402
        # port; AARCH64's errors only on the second UART (with no TPM
        # attached, Tcg2's "Not Found" errors are always there) and no INFO
        # line. RELEASE prints nothing on either.
        log, where = (debugcon, "debugcon 0x402") if s.arch == "x64" else (uart1, "the second UART")
        size = log.stat().st_size if log.is_file() else 0
        text = log.read_bytes().decode("latin-1") if size else ""
        info = text.count("Loading driver at")
        result.facts["debug_output"] = {"where": where, "bytes": size, "info_lines": info}
        if s.flavour == "RELEASE" and size:
            result.problems.append(f"{s.arch} RELEASE: {size} bytes of DEBUG output on {where}")
        elif s.flavour == "DEBUG" and s.arch == "x64" and not info:
            result.problems.append(f"x64 DEBUG: no verbose DEBUG output on {where} ({size} bytes)")
        elif s.flavour == "DEBUG" and s.arch == "aarch64" and (not size or info):
            result.problems.append(
                f"aarch64 DEBUG: {size} bytes on {where}, {info} INFO lines; errors only was expected"
            )

        def std_vga(g: Guest) -> None:
            g.prompt()
            got = hex_bytes(g.command(f"dmpstore {NV_NAME} -guid {NV_GUID}"))
            result.facts["nv_after_cold_start"] = got == token
            if got != token:
                result.problems.append(
                    f"{s.arch} {s.flavour}: after a cold start the variable reads {got or 'nothing'}, not {token}"
                )
            modes["std-vga"] = text_modes(g.command("mode"))
            w, h, lit = g.screen(Path(tmp) / "std-vga.ppm")
            result.facts["gop_std_vga"] = {"size": [w, h], "drawn": lit}
            if s.arch == "x64" and (w, h) != GOP_DEFAULT:
                result.problems.append(f"x64 {s.flavour}: std-VGA's GOP is {w}x{h}, not 1280x800")
            if not lit:
                result.problems.append(f"{s.arch} {s.flavour}: std-VGA shows nothing (a {w}x{h} screen, all black)")
            g.send("exit\r")
            g.expect(re.compile(r"Boot Maintenance Manager"), "front page after the Shell's exit")
            result.facts["front_page_after_exit"] = True

        attempt("std-vga", ["-device", _DEVICE["std-vga"]], std_vga)

        def virtio_gpu(g: Guest) -> None:
            g.prompt()
            modes["virtio-gpu"] = text_modes(g.command("mode"))

        attempt("virtio-gpu", ["-device", _DEVICE["virtio-gpu"]], virtio_gpu)

        def bochs(g: Guest) -> None:
            g.prompt()
            modes["bochs-display"] = text_modes(g.command("mode"))
            out = g.command("drivers")
            line = next((x for x in out.splitlines() if "QEMU Video Driver" in x), "")
            fields = line.split()
            # DRV VERSION TYPE CFG DIAG #D #C DRIVER-NAME...: #D, the devices managed.
            devices = int(fields[5]) if len(fields) > 6 and fields[5].isdigit() else 0
            w, h, lit = g.screen(Path(tmp) / "bochs.ppm")
            result.facts["bochs_display"] = {"devices": devices, "size": [w, h], "drawn": lit}
            if devices < 1 or (w, h) != GOP_DEFAULT or not lit:
                result.problems.append(
                    f"{s.arch} {s.flavour}: bochs-display has no working GOP (QemuVideoDxe manages "
                    f"{devices} devices; the display is {w}x{h}, {'drawn' if lit else 'all black'})"
                )

        attempt("bochs-display", ["-device", _DEVICE["bochs-display"]], bochs)

        if s.arch == "aarch64":
            cpus: dict[str, bool] = {}
            result.facts["cpu_models_reach_shell"] = cpus
            for cpu in AARCH64_CPUS:
                g = Guest(qemu_argv(qemu_dir, s, vars_copy, esp, cpu), workdir, Path(socks), f"{tag}-cpu-{cpu}", timeout)
                try:
                    g.prompt()
                    cpus[cpu] = True
                except BootError as err:
                    cpus[cpu] = False
                    result.problems.append(f"{s.arch} {s.flavour}: under -cpu {cpu}: {err}")
                finally:
                    g.close()
    for shape in SHAPES:
        if shape not in modes:
            result.problems.append(f"{s.arch} {s.flavour}: no text modes measured for {shape}")
    return result
