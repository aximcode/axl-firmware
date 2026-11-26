"""The TCP and virtio-fs boot checks: patches 0013 and 0011, per release.

tests/repro/'s ReproPkg holds two UEFI programs (build.sh builds them for
each arch, out of tree, never shipped):

- TcpHold.efi, a resident driver: it listens on port 5555 (the NIC given a
  static 10.0.2.15), accepts one connection from the host (QEMU's
  user-mode `hostfwd`), receives 6 bytes and keeps a Receive pending.
  The Shell's `reconnect -r` must then return within BOUND, both of
  TcpHold's sockets (the listener and the accepted one) gone; and the
  network must work after it (TcpHold loaded again accepts). Without patch
  0013 `reconnect -r` never returns (the core calls TcpDxe's Stop() for
  ever).
- VfsRace.efi, run from a virtio-fs share (virtiofsd): it reads a
  3000-byte file whole in a loop while a 1 ms TPL_CALLBACK timer does the
  same, until the timer has made 1000 rounds; every read must equal the
  first, and it prints `vfsrace: PASS`. Then `reconnect -r` stops and
  starts the share, which must list the file again. Without patch 0011 the
  guest hangs in VirtioFsDxe.

Each runs on the DEBUG and RELEASE sets, in a boot of its own, on the
boot checks' command line (AARCH64 `cortex-a57`), the TCP boot with a
virtio-rng: under `cortex-a57` (no RNDR) it is the network stack's only
RNG, which `reconnect -r` stops too; patch 0015 keeps a DEBUG image from
asserting then (DESIGN §4.38).

virtiofsd is AXLFW_VIRTIOFSD, else `virtiofsd` on PATH, else
/usr/libexec/virtiofsd (Fedora's, EL's, Debian's and Ubuntu's path). With
AXLFW_REQUIRE_VIRTIOFS=1 a missing one is a problem before any boot;
without, the virtio-fs check is skipped, said loudly, and the manifest
records `"vfs_race": "skipped"`.

tests/repro/repro.py runs the same functions by hand, on any image.

docs/DESIGN.md §4.39.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from axlfw import boot

TCP_APP = "TcpHold.efi"
VFS_APP = "VfsRace.efi"
FLAVOURS = ("DEBUG", "RELEASE")
# How long `reconnect -r` and VfsRace have: with the patches both took at
# most 10.2 s (VfsRace) and 0.7 s (reconnect) on every image set (local,
# AlmaLinux 10 x86_64, QEMU 10.0.0; X64 under KVM, AARCH64 under TCG);
# without them, never.
BOUND = 60.0
TCP4_GUID = "65530BC7-A359-410F-B010-5AADC7EC2B62"
PORT = 5555
# The share's file: 3000 bytes VfsRace reads, and the Shell's `ls` shows.
RACE_FILE = "race.txt"
RACE_BYTES = bytes((i * 7 + 3) % 251 for i in range(3000))
REQUIRE_ENV = "AXLFW_REQUIRE_VIRTIOFS"
VIRTIOFSD_ENV = "AXLFW_VIRTIOFSD"
_VFS_RESULT = re.compile(r"vfsrace: (PASS|FAIL|ERROR)[^\r\n]*(?=\r?\n)")
_TCPHOLD_SOCKET = re.compile(r"^\s*([0-9A-F]+): .*IPv4\(([0-9.]+)\)", re.MULTILINE)
_TCPHOLD_LISTENING = re.compile(r"tcphold: (listening on port 5555|ERROR[^\r\n]*)")


def app(ws: Path, toolchain: str, arch: str, name: str) -> Path:
    """Where build.sh's build of ReproPkg's name for arch is."""
    return ws / "Build" / "Repro" / f"DEBUG_{toolchain}" / arch.upper() / name


def tcp_app(ws: Path, toolchain: str, arch: str) -> Path:
    """Where build.sh's build of TcpHold.efi for arch is."""
    return app(ws, toolchain, arch, TCP_APP)


def vfs_app(ws: Path, toolchain: str, arch: str) -> Path:
    """Where build.sh's build of VfsRace.efi for arch is."""
    return app(ws, toolchain, arch, VFS_APP)


def virtiofsd() -> Path | None:
    """The virtiofsd to use, if there is one (the module's docstring)."""
    named = os.environ.get(VIRTIOFSD_ENV, "")
    if named:
        return Path(named)
    found = shutil.which("virtiofsd")
    if found:
        return Path(found)
    libexec = Path("/usr/libexec/virtiofsd")
    return libexec if os.access(libexec, os.X_OK) else None


def required() -> bool:
    """Whether AXLFW_REQUIRE_VIRTIOFS asks for the virtio-fs check."""
    return os.environ.get(REQUIRE_ENV, "") == "1"


def required_problems(given: bool) -> list[str]:
    """The virtio-fs check required, where its application is not given."""
    if given or not required():
        return []
    return [
        f"{REQUIRE_ENV}=1 requires the virtio-fs boot check, which needs {VFS_APP}"
        " (a build has it; `build.sh check` does not)"
    ]


def virtiofsd_problems(fsd: Path | None) -> list[str]:
    """A virtiofsd named but not executable, or none where one is required."""
    if fsd is not None:
        return [] if os.access(fsd, os.X_OK) else [f"{fsd}: virtiofsd is not an executable file"]
    if required():
        return [
            f"virtiofsd not found ({VIRTIOFSD_ENV}, PATH or /usr/libexec/virtiofsd):"
            f" {REQUIRE_ENV}=1 requires the virtio-fs boot check"
        ]
    return []


def free_port() -> int:
    """A free TCP port on the loopback."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def reconnect(g: boot.Guest, bound: float = BOUND) -> float | None:
    """`reconnect -r`; the seconds it took, or None if it never returned."""
    start = time.monotonic()
    g.send("reconnect -r\r")
    try:
        g.expect(boot.PROMPT, "prompt after reconnect -r", bound)
    except boot.BootError:
        return None
    return time.monotonic() - start


def tcphold_sockets(dh: str) -> list[tuple[str, str]]:
    """TcpHold's sockets in `dh -p <TCP4>`'s output: the TCP4 handles with
    an IPv4 device path (the listener's 0.0.0.0, the accepted one's the
    peer's). Another driver's TCP4 child, with no device path, is left out."""
    found: list[tuple[str, str]] = _TCPHOLD_SOCKET.findall(dh)
    return found


def load_tcphold(g: boot.Guest) -> None:
    """Give the NIC its address and load the server; BootError if it fails."""
    g.command("ifconfig -s eth0 static 10.0.2.15 255.255.255.0 10.0.2.2")
    g.send(f"load fs0:\\{TCP_APP}\r")
    m = g.expect(_TCPHOLD_LISTENING, "TcpHold listening")
    g.expect(boot.PROMPT, f"prompt after loading {TCP_APP}")
    if m.group(1).startswith("ERROR"):
        msg = f"{g.name}: {m.group(0)}"
        raise boot.BootError(msg)


def tcp_reconnect(g: boot.Guest, port: int, bound: float = BOUND) -> tuple[float, list[tuple[str, str]]]:
    """TcpHold holding an accepted connection, then `reconnect -r`.

    Returns the seconds it took and TcpHold's sockets before it. BootError
    when it does not return within bound, a socket outlives it, or the
    network does not work after it.
    """
    load_tcphold(g)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(b"hello\n")
        g.expect(re.compile(r"tcphold: accepted"), "TcpHold accepting the host's connection", 30)
        g.expect(re.compile(r"tcphold: received 6 bytes"), "TcpHold receiving 6 bytes", 30)
        before = tcphold_sockets(g.command(f"dh -p {TCP4_GUID}"))
        if len(before) != 2:
            msg = f"{g.name}: TcpHold's sockets before reconnect -r are {before}, not a listener and one accepted"
            raise boot.BootError(msg)
        took = reconnect(g, bound)
        if took is None:
            msg = (
                f"{g.name}: reconnect -r did not return within {bound:.0f} s while TcpHold held an accepted"
                f" connection (sockets {before})"
            )
            raise boot.BootError(msg)
        after = tcphold_sockets(g.command(f"dh -p {TCP4_GUID}"))
    if after:
        msg = f"{g.name}: TcpHold's sockets after reconnect -r are {after}, not none"
        raise boot.BootError(msg)
    # The NIC's media and address come back; then a new server accepts.
    time.sleep(3)
    load_tcphold(g)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as again:
        again.sendall(b"again\n")
        g.expect(re.compile(r"tcphold: received 6 bytes"), "a connection to TcpHold after reconnect -r", 30)
    return took, before


def find_share(g: boot.Guest) -> str | None:
    """The Shell volume holding the race file, whole, after a `connect -r`."""
    g.command("connect -r")
    for n in range(6):
        listing = g.command(f"ls fs{n}:\\{RACE_FILE}")
        if RACE_FILE in listing and f"{len(RACE_BYTES):,}" in listing:
            return f"fs{n}:"
    return None


def vfs_race(g: boot.Guest, bound: float = BOUND) -> tuple[str, float]:
    """VfsRace from the share, then the share stopped and started.

    Returns VfsRace's PASS line and the seconds it took. BootError when the
    share is not found, VfsRace does not pass within bound, `reconnect -r`
    does not return, or the share does not list the file after it.
    """
    vol = find_share(g)
    if vol is None:
        msg = f"{g.name}: no virtio-fs volume holds {RACE_FILE}"
        raise boot.BootError(msg)
    g.send(f"{vol}\r")
    g.expect(boot.PROMPT, "the prompt on the share")
    start = time.monotonic()
    g.send(f"{VFS_APP}\r")
    m = g.expect(_VFS_RESULT, f"{VFS_APP}'s result", bound)
    took = time.monotonic() - start
    if m.group(1) != "PASS":
        msg = f"{g.name}: {m.group(0)}"
        raise boot.BootError(msg)
    g.expect(boot.PROMPT, f"the prompt after {VFS_APP}")
    # The driver's Stop() and Start() (patch 0011 locks Stop): the share
    # is disconnected and connected again, and still reads.
    g.command("fs0:")
    if reconnect(g, bound) is None:
        msg = f"{g.name}: reconnect -r did not return within {bound:.0f} s after {VFS_APP}"
        raise boot.BootError(msg)
    # The share may come back under another number.
    g.command("map -r")
    if find_share(g) is None:
        msg = f"{g.name}: after reconnect -r, no virtio-fs volume holds {RACE_FILE}"
        raise boot.BootError(msg)
    return m.group(0), took


def esp_with(s: boot.ImageSet, t: Path, files: list[Path]) -> tuple[Path, Path]:
    """A boot disk in t holding s's Shell and files, and a copy of s's vars."""
    esp = t / "esp"
    boot_dir = esp / "EFI" / "BOOT"
    boot_dir.mkdir(parents=True)
    shutil.copyfile(s.shell, boot_dir / boot.BOOT_FILE[s.arch])
    for f in files:
        shutil.copyfile(f, esp / f.name)
    vars_copy = t / "vars.fd"
    shutil.copyfile(s.vars, vars_copy)
    return esp, vars_copy


def tcp_argv(
    qemu_dir: Path | None, s: boot.ImageSet, vars_copy: Path, esp: Path, port: int, cpu: str = boot.AARCH64_CPU
) -> list[str]:
    """The boot checks' command line with a user-mode NIC forwarding the
    host's port to the guest's 5555, and a virtio-rng (cpu: AARCH64's)."""
    return [
        *boot.qemu_argv(qemu_dir, s, vars_copy, esp, cpu),
        "-netdev",
        f"user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:{PORT}",
        "-device",
        "virtio-net-pci,netdev=n0,romfile=",
        "-device",
        "virtio-rng-pci",
    ]


def vfs_argv(qemu_dir: Path | None, s: boot.ImageSet, vars_copy: Path, esp: Path, sock: Path) -> list[str]:
    """The boot checks' command line with a virtio-fs share on sock, in
    shared memory (vhost-user needs it) of the guest's size."""
    return [
        *boot.qemu_argv(qemu_dir, s, vars_copy, esp),
        "-object",
        "memory-backend-memfd,id=mem,size=1024M,share=on",
        "-numa",
        "node,memdev=mem",
        "-chardev",
        f"socket,id=vfs,path={sock}",
        "-device",
        "vhost-user-fs-pci,queue-size=1024,chardev=vfs,tag=share",
    ]


def start_virtiofsd(fsd: Path, share: Path, sock: Path, log: Path) -> subprocess.Popen[bytes]:
    """virtiofsd serving share on sock, once its socket is there; BootError
    if it cannot start or makes none within 5 s."""
    with log.open("wb") as stderr:
        try:
            # --cache=never would drop FUSE readdirplus, which VirtioFsDxe needs.
            proc = subprocess.Popen(  # noqa: S603 - argv is ours
                [str(fsd), f"--socket-path={sock}", f"--shared-dir={share}", "--sandbox=none", "--cache=auto"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        except OSError as err:
            msg = f"cannot start {fsd}: {err.strerror}"
            raise boot.BootError(msg) from err
    deadline = time.monotonic() + 5
    while not sock.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            stop(proc)
            msg = f"virtiofsd made no socket ({log}: {log.read_text(errors='replace').strip()!r})"
            raise boot.BootError(msg)
        time.sleep(0.05)
    return proc


def stop(proc: subprocess.Popen[bytes]) -> None:
    """A helper process of ours ended: asked, then killed."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc.wait()


def check_tcp(s: boot.ImageSet, tcphold: Path, qemu_dir: Path | None, workdir: Path, timeout: float) -> boot.Result:
    """s's TCP boot: `reconnect -r` with an accepted connection held.

    The result's facts: {"tcp_reconnect": bool}.
    """
    result = boot.Result()
    result.facts["tcp_reconnect"] = False
    workdir.mkdir(parents=True, exist_ok=True)
    tag = f"{s.arch}-{s.flavour.lower()}-tcp"
    with tempfile.TemporaryDirectory(dir=workdir) as tmp, tempfile.TemporaryDirectory(prefix="axlfw-") as socks:
        esp, vars_copy = esp_with(s, Path(tmp), [tcphold])
        port = free_port()
        try:
            g = boot.Guest(tcp_argv(qemu_dir, s, vars_copy, esp, port), workdir, Path(socks), tag, timeout)
        except boot.BootError as err:
            result.problems.append(f"{s.arch} {s.flavour}: {err}")
            return result
        try:
            g.prompt()
            tcp_reconnect(g, port)
            result.facts["tcp_reconnect"] = True
        except (boot.BootError, OSError) as err:
            result.problems.append(f"{s.arch} {s.flavour}: the TCP reconnect check (patch 0013): {err}")
        finally:
            g.close()
    return result


def check_vfs(
    s: boot.ImageSet, vfsrace: Path, fsd: Path, qemu_dir: Path | None, workdir: Path, timeout: float
) -> boot.Result:
    """s's virtio-fs boot: VfsRace from a share, then `reconnect -r`.

    The result's facts: {"vfs_race": "pass"} when it passed, else "fail".
    """
    result = boot.Result()
    result.facts["vfs_race"] = "fail"
    workdir.mkdir(parents=True, exist_ok=True)
    tag = f"{s.arch}-{s.flavour.lower()}-vfs"
    with tempfile.TemporaryDirectory(dir=workdir) as tmp, tempfile.TemporaryDirectory(prefix="axlfw-") as socks:
        t = Path(tmp)
        esp, vars_copy = esp_with(s, t, [])
        share = t / "share"
        share.mkdir()
        shutil.copyfile(vfsrace, share / VFS_APP)
        (share / RACE_FILE).write_bytes(RACE_BYTES)
        sock = Path(socks) / "fs.sock"
        try:
            proc = start_virtiofsd(fsd, share, sock, workdir / f"{tag}-virtiofsd.log")
        except boot.BootError as err:
            result.problems.append(f"{s.arch} {s.flavour}: {err}")
            return result
        try:
            g = boot.Guest(vfs_argv(qemu_dir, s, vars_copy, esp, sock), workdir, Path(socks), tag, timeout)
            try:
                g.prompt()
                vfs_race(g)
                result.facts["vfs_race"] = "pass"
            except boot.BootError as err:
                result.problems.append(f"{s.arch} {s.flavour}: the virtio-fs check (patch 0011): {err}")
            finally:
                g.close()
        except boot.BootError as err:
            result.problems.append(f"{s.arch} {s.flavour}: {err}")
        finally:
            stop(proc)
    return result
