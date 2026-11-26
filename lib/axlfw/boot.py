"""The release's boot checks: one image set booted in QEMU, its Shell driven
over the serial console.

Per arch and flavour, six boots (and on AARCH64 one per CPU model), each
from a FAT disk whose removable path holds the release's own Shell:

- serial only: the Shell's `mode` list; a variable written (`setvar -nv`),
  a warm reset, the variable read back (`dmpstore`); SMBIOS type 0's BIOS
  version (`smbiosview -t 0`); X64's debugcon output; then `reset -s`;
- std-VGA, a new QEMU on the same vars file: the variable read back after
  a cold start; the `mode` list; the GOP's resolution (a screendump); then
  the Shell's `exit`, which must reach the front page;
- virtio-gpu: the `mode` list;
- bochs-display, without its EDID: the `mode` list, and the GOP
  QemuVideoDxe gives it (its driver managing a device, and a screendump at
  the firmware's default resolution);
- serial only, again: a script that ticks for a minute, broken by a
  serial Ctrl-C (0x03), which must bring the prompt back within
  CTRL_C_BOUND (patch 0004);
- a NIC and virtio-rng, on a CPU with no RNG instruction: `reconnect -r`,
  which must come back with no ASSERT and the network working (patch
  0015).

X64 runs under KVM when /dev/kvm is usable, else TCG; AARCH64 under TCG.
"""

from __future__ import annotations

import contextlib
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
# std-VGA and bochs-display without their EDID: QemuVideoDxe takes an EDID's
# preferred mode over PcdVideoHorizontalResolution/Vertical (for the whole
# MMIO variant, both devices), and QEMU's default EDID asks 1280x800 too, so
# with it the checks would measure QEMU, not the firmware's default.
_DEVICE = {
    "std-vga": "VGA,edid=off",
    "virtio-gpu": "virtio-gpu-pci",
    "bochs-display": "bochs-display,edid=off",
}
# A serial Ctrl-C (0x03) must stop a running Shell script and bring the
# prompt back within this many seconds. Measured with patch 0004 (AlmaLinux
# 10 x86_64, QEMU 10.0.0): 0.10 s on all four image sets, X64 under KVM and
# AARCH64 under TCG alike; without it the script runs to its end, a minute
# later. 10 s, axl-sdk's bound for the same keystroke, leaves a loaded TCG
# host a hundredfold margin and is still a sixth of the script.
CTRL_C_BOUND = 10.0
# The script the Ctrl-C breaks, on the boot disk: a tick every 0.1 s for at
# least a minute, then "ticks done". The Shell checks for a break between
# a script's lines.
TICKS_SCRIPT = "ticks.nsh"
TICKS = (
    "@echo -off\r\n"
    "for %a run (1 600)\r\n"
    "  echo tick %a\r\n"
    "  stall 100000\r\n"
    "endfor\r\n"
    "echo ticks done\r\n"
)
# `reconnect -r` (every driver stopped, then every controller connected)
# with virtio-rng the only EFI_RNG_PROTOCOL: on a CPU with no RNG
# instruction (AARCH64 cortex-a57, no FEAT_RNG; X64 without RDRAND and
# RDSEED) the network drivers then start before VirtioRngDxe, and before
# patch 0015 a DEBUG image asserted in DxeNetLib. The prompt must come back
# within RECONNECT_BOUND, with no ASSERT on the debug channel, eth0 listed
# again, and a DHCP lease from QEMU's user network (its first, NET_LEASE)
# within DHCP_BOUND, which needs Dhcp4, Udp4, Ip4 and MNP started again.
# A DEBUG image must also show NO_RNG_ERROR: a network driver did start
# without an RNG, so the boot tested the window it is for.
# Measured with patch 0015 (AlmaLinux 10 x86_64, QEMU 10.0.0): DESIGN §4.33
# ("Patch 0015").
RECONNECT_BOUND = 60.0
DHCP_BOUND = 30.0
NET_LEASE = "10.0.2.15"
# DxeNetLib's error when a network driver starts with no EFI_RNG_PROTOCOL.
NO_RNG_ERROR = "Failed to locate EFI_RNG_PROTOCOL"
RECONNECT_AARCH64_CPU = "cortex-a57"
RECONNECT_X64_CPU_FLAGS = ",-rdrand,-rdseed"
_NET = ["-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0,romfile=", "-device", "virtio-rng-pci"]
# DebugLib's assertion line (DebugAssert): "ASSERT [Module] File(Line): Expr"
# on AARCH64, "ASSERT File(Line): Expr" on X64.
_ASSERT = re.compile(r"^ASSERT (?:\[[^\]\n]+\] )?\S+\(\d+\): .*$", re.MULTILINE)
_ETH0 = re.compile(r"^name\s+: eth0$", re.MULTILINE)
_IPV4 = re.compile(r"^ipv4 address : (\S+)$", re.MULTILINE)
# The longest keep_state waits on one monitor command: a wedged monitor costs
# a boot given up on this much more, then close()'s own wait.
STATE_TIMEOUT = 5.0
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[0-9A-Za-z=>]")
_PROMPT = re.compile(r"(?:Shell|FS\d+:[^>\n]*)> ")
_PPM = re.compile(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s(.*)\Z", re.DOTALL)
_MODE = re.compile(r"Col\s+(\d+)\s+Row\s+(\d+)\s+(\*?)")


class BootError(Exception):
    """A boot did not do what the check needed: said with its transcript."""


def _hmp_reply(raw: str) -> str:
    """An HMP reply without the monitor's echo of the line (the first line,
    drawn with cursor moves), its CRs or the next prompt."""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw).replace("\r", "")
    text = text.removesuffix("(qemu) ")
    return text.split("\n", 1)[1] if "\n" in text else ""


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
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - argv is ours
                full, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr
            )
        except OSError as err:
            self._stderr.close()
            msg = f"{name}: cannot start {argv[0]}: {err.strerror}"
            raise BootError(msg) from err
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
                    break
                self._cond.wait(min(left, 0.5))
        # Outside the lock: the reader goes on while the monitor is asked.
        # The record never replaces the BootError, and a QEMU that exits
        # while it is taken is still said.
        if self.proc.poll() is None:
            try:
                tail += f"\n(QEMU's state when it was given up on: {self.keep_state()})"
            except OSError as err:
                tail += f"\n(QEMU's state not kept: {err})"
        if self.proc.poll() is not None:
            err = self.stderr_path.read_text(errors="replace").strip()
            tail += f"\nQEMU exited {self.proc.returncode}: {err}"
        msg = f"{self.name}: no {what} within {timeout or self.timeout:.0f} s; console ends:\n{tail}"
        raise BootError(msg)

    def keep_state(self) -> Path:
        """QEMU's state, from its monitor, kept beside the transcript as
        <name>.monitor: for a guest given up on, to tell where it stopped.

        The vCPUs' registers twice (whether they still move), and each virtio
        queue as QEMU sees it (`info virtio-queue-status`) and as the guest
        left it (the avail and used rings' indices, read from guest memory):
        a request the guest made that QEMU never took shows as an avail index
        past QEMU's last_avail_idx. A monitor that fails, or takes more than
        STATE_TIMEOUT seconds over a command, ends the record, said.
        """
        path = self.log_path.with_suffix(".monitor")
        record: list[str] = []

        def ask(line: str, keep: bool = True) -> str:
            try:
                reply = _hmp_reply(self.monitor(line, STATE_TIMEOUT))
            except BootError as err:
                record.append(f"### {line}\n{err}\n")
                raise
            if keep:
                record.append(f"### {line}\n{reply}")
            return reply

        try:
            ask("info status")
            ask("info registers -a")
            time.sleep(0.5)
            ask("info registers -a")
            for dev in re.findall(r"^(/\S+)", ask("info virtio"), re.MULTILINE):
                m = re.search(r"num_vqs:\s*(\d+)", ask(f"info virtio-status {dev}", keep=False))
                for q in range(int(m.group(1)) if m else 0):
                    queue = ask(f"info virtio-queue-status {dev} {q}")
                    for ring in ("avail", "used"):
                        a = re.search(rf"^\s*{ring}:\s*0x([0-9a-fA-F]+)", queue, re.MULTILINE)
                        if a and int(a.group(1), 16):
                            # The ring's idx, after its 16-bit flags (a
                            # queue the guest never set up is at 0).
                            ask(f"xp /1hx {int(a.group(1), 16) + 2:#x}")
        except BootError:
            pass  # said in the record, by ask
        path.write_text("".join(record))
        return path

    @property
    def mark(self) -> int:
        """Where the next expect starts looking in the console."""
        with self._cond:
            return self._mark

    def output(self, start: int, end: int) -> str:
        """The console between two positions (a mark, a match's start)."""
        with self._cond:
            return self._text[start:end]

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
        start = self.mark
        m = self.expect(_PROMPT, f"prompt after `{line}`")
        out = self.output(start, m.start())
        # Drop the command's own echo.
        return out.split("\n", 1)[1] if "\n" in out else ""

    def monitor(self, line: str, timeout: float = 30) -> str:
        """One HMP command; its reply. A QEMU gone is a BootError."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
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
        """Stop QEMU if it is still running: asked to quit, else killed.

        Whatever the monitor does, QEMU is reaped and its files closed.
        """
        try:
            if self.proc.poll() is None:
                with contextlib.suppress(BootError, subprocess.TimeoutExpired):
                    self.monitor("quit")
                    self.proc.wait(10)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
            self._reader.join(10)
            for f in (self.proc.stdin, self.proc.stdout):
                if f is not None:
                    with contextlib.suppress(OSError):
                        f.close()
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


def qemu_binary(arch: str) -> str:
    """The QEMU system emulator arch's images boot in."""
    return "qemu-system-x86_64" if arch == "x64" else "qemu-system-aarch64"


def missing_qemu(qemu_dir: Path | None, arches: tuple[str, ...]) -> list[str]:
    """Each QEMU the boots of arches need and cannot find, said."""
    problems: list[str] = []
    for arch in arches:
        binary = qemu_binary(arch)
        if qemu_dir:
            if not os.access(qemu_dir / binary, os.X_OK):
                problems.append(f"{qemu_dir}: no executable {binary} (QEMU_DIR, or --qemu-dir)")
        elif not shutil.which(binary):
            problems.append(f"{binary} not found on PATH (or set QEMU_DIR)")
    return problems


def qemu_argv(qemu_dir: Path | None, s: ImageSet, vars_copy: Path, esp: Path, cpu: str = AARCH64_CPU) -> list[str]:
    """The QEMU command line shared by every boot of s (cpu: AARCH64's)."""
    binary = qemu_binary(s.arch)
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


def ctrl_c_break(g: Guest, bound: float = CTRL_C_BOUND) -> float:
    """Start the ticks script, send a serial Ctrl-C while it runs, and wait
    for the prompt; the seconds it took. BootError if the prompt did not
    come within bound, came because the script ran to its end or because
    the firmware restarted, or the Shell then does not run a command.
    """
    g.send(f"fs0:\\{TICKS_SCRIPT}\r")
    g.expect(re.compile(r"\btick 3\b"), "the ticks script running")
    sent = time.monotonic()
    g.send("\x03")
    start = g.mark
    m = g.expect(_PROMPT, "Shell prompt after a serial Ctrl-C (0x03) to a running script", bound)
    took = time.monotonic() - sent
    between = g.output(start, m.start())
    if "ticks done" in between:
        msg = f"{g.name}: a serial Ctrl-C (0x03) did not stop the script: it ran to its end"
        raise BootError(msg)
    if "Press ESC in" in between or "UEFI Interactive Shell" in between:
        msg = f"{g.name}: the firmware restarted after a serial Ctrl-C (0x03), instead of breaking the script"
        raise BootError(msg)
    if "ctrl-c-ok" not in g.command("echo ctrl-c-ok"):
        msg = f"{g.name}: after a serial Ctrl-C (0x03) the Shell did not run `echo`"
        raise BootError(msg)
    return took


def first_assert(text: str) -> str | None:
    """The first DebugLib assertion line in a debug log, if any."""
    m = _ASSERT.search(text.replace("\r", ""))
    return m.group(0) if m else None


def _debug_text(debug_log: Path) -> str:
    return debug_log.read_bytes().decode("latin-1") if debug_log.is_file() else ""


def reconnect_argv(argv: list[str], arch: str, debug_log: Path) -> list[str]:
    """argv for the reconnect boot: a CPU with no RNG instruction, the debug
    channel to debug_log, a NIC on QEMU's user network, and virtio-rng."""
    out = list(argv)
    i = out.index("-cpu") + 1
    if arch == "x64":
        out[i] += RECONNECT_X64_CPU_FLAGS
        debug = ["-debugcon", f"file:{debug_log}", "-global", "isa-debugcon.iobase=0x402"]
    else:
        out[i] = RECONNECT_AARCH64_CPU
        debug = ["-serial", f"file:{debug_log}"]
    return [*out, *debug, *_NET]


def reconnect_network(
    g: Guest, debug_log: Path, debug_flavour: bool, bound: float = RECONNECT_BOUND, dhcp_bound: float = DHCP_BOUND
) -> float:
    """Run `reconnect -r` with eth0 up; the seconds it took. BootError if eth0
    is not listed before it, the prompt does not come back within bound, the
    firmware asserted, a DEBUG image's debug channel does not show a network
    driver started without an RNG during it (DxeNetLib's error: else it
    tested nothing), eth0 is not listed after it, or eth0 gets no DHCP lease
    (NET_LEASE) within dhcp_bound.
    """
    if not _ETH0.search(g.command("ifconfig -l")):
        msg = f"{g.name}: `ifconfig -l` lists no eth0 before `reconnect -r`"
        raise BootError(msg)
    start = len(_debug_text(debug_log))
    g.send("reconnect -r\r")
    sent = time.monotonic()
    try:
        g.expect(_PROMPT, "prompt after `reconnect -r`", bound)
    except BootError as err:
        line = first_assert(_debug_text(debug_log)[start:])
        if line:
            msg = f"{g.name}: the firmware asserted in `reconnect -r`: {line}"
            raise BootError(msg) from err
        raise
    took = time.monotonic() - sent
    debug = _debug_text(debug_log)
    line = first_assert(debug[start:])
    if line:
        msg = f"{g.name}: the firmware asserted in `reconnect -r`: {line}"
        raise BootError(msg)
    if debug_flavour and NO_RNG_ERROR not in debug[start:]:
        msg = (
            f"{g.name}: no {NO_RNG_ERROR!r} on the debug channel in `reconnect -r`: the network stack "
            "never started without an RNG, so it tested nothing"
        )
        raise BootError(msg)
    if not _ETH0.search(g.command("ifconfig -l")):
        msg = f"{g.name}: `ifconfig -l` lists no eth0 after `reconnect -r`"
        raise BootError(msg)
    g.command("ifconfig -s eth0 dhcp")
    deadline = time.monotonic() + dhcp_bound
    while True:
        m = _IPV4.search(g.command("ifconfig -l eth0"))
        addr = m.group(1) if m else "none"
        if addr == NET_LEASE:
            return took
        if time.monotonic() >= deadline:
            msg = (
                f"{g.name}: eth0 has no DHCP lease ({NET_LEASE}) within {dhcp_bound:.0f} s "
                f"after `reconnect -r`: its ipv4 address is {addr}"
            )
            raise BootError(msg)
        time.sleep(0.5)


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
        (esp / TICKS_SCRIPT).write_bytes(TICKS.encode())
        vars_copy = t / "vars.fd"
        shutil.copyfile(s.vars, vars_copy)
        base = qemu_argv(qemu_dir, s, vars_copy, esp)
        tag = f"{s.arch}-{s.flavour.lower()}"
        token = secrets.token_hex(8)
        modes: dict[str, object] = {}
        result.facts["text_modes"] = modes

        def attempt(name: str, extra: list[str], body: Callable[[Guest], None], argv: list[str] | None = None) -> None:
            try:
                g = Guest([*(argv or base), *extra], workdir, Path(socks), f"{tag}-{name}", timeout)
            except BootError as err:
                result.problems.append(f"{s.arch} {s.flavour}: {err}")
                return
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
            if (w, h) != GOP_DEFAULT:
                result.problems.append(f"{s.arch} {s.flavour}: std-VGA's GOP is {w}x{h}, not 1280x800")
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

        def ctrl_c(g: Guest) -> None:
            g.prompt()
            ctrl_c_break(g)

        attempt("ctrl-c", [], ctrl_c)

        # Its own vars copy: `ifconfig -s eth0 dhcp` keeps the policy in NV.
        net_vars = t / "net-vars.fd"
        shutil.copyfile(s.vars, net_vars)
        net_debug = workdir / f"{tag}-reconnect-debug.log"
        net_debug.unlink(missing_ok=True)

        result.facts["reconnect_network"] = False

        def reconnect(g: Guest) -> None:
            g.prompt()
            reconnect_network(g, net_debug, s.flavour == "DEBUG")
            result.facts["reconnect_network"] = True

        attempt("reconnect", [], reconnect, reconnect_argv(qemu_argv(qemu_dir, s, net_vars, esp), s.arch, net_debug))

        if s.arch == "aarch64":
            cpus: dict[str, bool] = {}
            result.facts["cpu_models_reach_shell"] = cpus
            for cpu in AARCH64_CPUS:
                cpus[cpu] = False

                def reaches_shell(g: Guest, cpu: str = cpu) -> None:
                    try:
                        g.prompt()
                    except BootError as err:
                        msg = f"under -cpu {cpu}: {err}"
                        raise BootError(msg) from err
                    cpus[cpu] = True

                attempt(f"cpu-{cpu}", [], reaches_shell, qemu_argv(qemu_dir, s, vars_copy, esp, cpu))
    for shape in SHAPES:
        if shape not in modes:
            result.problems.append(f"{s.arch} {s.flavour}: no text modes measured for {shape}")
    return result
