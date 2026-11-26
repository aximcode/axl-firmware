"""Reproducers of r3's patches 0011 (VirtioFsDxe), 0012 (LocalApic EOI)
and 0013 (TcpDxe), run by hand against any image (DESIGN §4.28-4.30).

    python3 -I tests/repro/repro.py <vfs|tcp|idle> <x64|aarch64> <code.fd> <vars.fd>
        --shell Shell_<ARCH>.efi --apps <dir from build-apps.sh> --out <dir>
        [--qemu-dir DIR] [--virtiofsd PATH] [--cpu MODEL] [--no-server] [--seconds N]

Each boots the image on the boot checks' QEMU command line
(lib/axlfw/boot.py) with the Shell on a FAT boot disk, and prints one
verdict line:

- vfs: a virtio-fs share (virtiofsd) holding VfsRace.efi and race.txt;
  VfsRace runs from the share, reading race.txt in the foreground and from
  a TPL_CALLBACK timer at once. PASS: `vfsrace: PASS` within BOUND.
  Without patch 0011 the guest hangs in VirtioFsDxe.
- tcp: a user-mode NIC with a host forward to guest port 5555 and a
  virtio-rng; TcpHold.efi, a resident server, accepts a host connection
  and holds it; then `reconnect -r`. GREEN: the prompt back within BOUND,
  TcpHold's two sockets (listener and accepted) gone, and the network
  usable again (TcpHold loaded again accepts). Without patch 0013 the
  reconnect never returns. --no-server is the control: the same boot and
  reconnect with no server.
- idle: KVM's exit counters (HMP `info stats vcpu`) over --seconds of an
  idle Shell, per second; patch 0012 lowers them.

On a failure, QEMU's registers, 40 samples of the PC and (X64) the frame
chain go to <out>/<name>.*; the X64 debug log (debugcon) or AARCH64's
second UART is <out>/<name>.debugcon or .uart1.
"""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from axlfw import boot  # noqa: E402

BOUND = 60.0
TCP4_GUID = "65530BC7-A359-410F-B010-5AADC7EC2B62"
PROMPT = boot._PROMPT  # pyright: ignore[reportPrivateUsage]
hmp_reply = boot._hmp_reply  # pyright: ignore[reportPrivateUsage]


def say(msg: str) -> None:
    """One line of the run's record."""
    print(msg, flush=True)


def free_port() -> int:
    """A free TCP port on the loopback."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def evidence(g: boot.Guest, out: Path, name: str, arch: str) -> None:
    """Where a failed guest is: registers, PC samples, the frame chain."""
    if g.proc.poll() is not None:
        return
    samples: list[str] = []
    for _ in range(40):
        m = re.search(r"(?:RIP|PC)=\s*([0-9a-f]+)", g.monitor("info registers", timeout=5))
        if m:
            samples.append(m.group(1))
        time.sleep(0.05)
    (out / f"{name}.samples").write_text("\n".join(samples) + "\n")
    regs = g.monitor("info registers", timeout=5)
    (out / f"{name}.regs").write_text(regs)
    rip = re.search(r"(?:RIP|PC)=\s*([0-9a-f]+)", regs)
    rbp = re.search(r"RBP=([0-9a-f]+)", regs)
    say(f"{name}: at failure PC {rip.group(1) if rip else 'unknown'}; evidence in {out}/{name}.*")
    if arch != "x64" or not rbp:
        return
    frames = [rip.group(1)] if rip else []
    fp = int(rbp.group(1), 16)
    for _ in range(16):
        words = re.findall(r"0x([0-9a-f]{16})", hmp_reply(g.monitor(f"xp /2gx {fp:#x}", timeout=5)))
        if len(words) < 2 or int(words[0], 16) <= fp:
            break
        frames.append(words[1])
        fp = int(words[0], 16)
    (out / f"{name}.frames").write_text("\n".join(frames) + "\n")


def vcpu_stats(g: boot.Guest) -> dict[str, dict[str, int]]:
    """KVM's cumulative per-vCPU counters, per CPU."""
    stats: dict[str, dict[str, int]] = {}
    for cpu in ("0", "1"):
        g.monitor(f"cpu {cpu}", timeout=5)
        r = hmp_reply(g.monitor("info stats vcpu", timeout=5))
        stats[cpu] = {k: int(v) for k, v in re.findall(r"^\s*(\w+) \(cumulative\): (\d+)", r, re.M)}
    return stats


def run_idle(g: boot.Guest, name: str, seconds: float) -> int:
    """Exits per second of an idle Shell, per vCPU, after 10 s to settle."""
    time.sleep(10)
    a = vcpu_stats(g)
    t0 = time.monotonic()
    time.sleep(seconds)
    b = vcpu_stats(g)
    dt = time.monotonic() - t0
    keys = ("exits", "halt_exits", "io_exits", "mmio_exits", "irq_exits")
    for cpu, before in a.items():
        rates = {k: round((b[cpu][k] - before[k]) / dt, 1) for k in keys if k in before}
        say(f"{name}: vcpu{cpu} per second over {dt:.1f} s: {rates}")
    return 0


def run_vfs(g: boot.Guest, name: str) -> int:
    """VfsRace from the share; 0 on PASS."""
    g.command("connect -r")
    vol = None
    for n in range(6):
        listing = g.command(f"ls fs{n}:\\race.txt")
        if "race.txt" in listing and "Not Found" not in listing:
            vol = f"fs{n}:"
            break
    if vol is None:
        say(f"{name}: SETUP no virtio-fs volume holds race.txt")
        return 2
    g.send(f"{vol}\r")
    g.expect(PROMPT, "prompt on the share")
    start = time.monotonic()
    g.send("VfsRace.efi\r")
    try:
        m = g.expect(re.compile(r"vfsrace: (PASS|FAIL|ERROR)[^\r\n]*(?=\r?\n)"), "vfsrace result", BOUND)
    except boot.BootError:
        say(f"{name}: RED-HANG no vfsrace result within {BOUND:.0f} s")
        return 1
    say(f"{name}: {m.group(0)} after {time.monotonic() - start:.1f} s")
    if m.group(1) != "PASS":
        return 1
    # The driver's Stop() and Start() (patch 0011 locks Stop): the share
    # is disconnected and connected again, and still reads.
    g.expect(PROMPT, "prompt after VfsRace")
    g.command("fs0:")
    if reconnect(g) is None:
        say(f"{name}: RED-HANG reconnect -r after VfsRace")
        return 1
    g.command("map -r")
    listing = g.command(f"ls {vol}\\race.txt")
    if "3,000" not in listing:
        say(f"{name}: after reconnect -r, {vol} does not list race.txt: {listing.strip()!r}")
        return 1
    say(f"{name}: after reconnect -r the share lists race.txt again")
    return 0


def tcphold_sockets(g: boot.Guest) -> list[tuple[str, str]]:
    """TcpHold's sockets: the TCP4 handles with an IPv4 device path (its
    listener's is 0.0.0.0, the accepted one's 10.0.2.2). Another driver's
    TCP4 child, with no device path, is left out."""
    found: list[tuple[str, str]] = re.findall(
        r"^\s*([0-9A-F]+): .*IPv4\(([0-9.]+)\)", g.command(f"dh -p {TCP4_GUID}"), re.M
    )
    return found


def load_tcphold(g: boot.Guest) -> None:
    """Give the NIC its address and load the server."""
    g.command("ifconfig -s eth0 static 10.0.2.15 255.255.255.0 10.0.2.2")
    g.send("load fs0:\\TcpHold.efi\r")
    m = g.expect(re.compile(r"tcphold: (listening on port 5555|ERROR[^\r\n]*)"), "tcphold listening")
    g.expect(PROMPT, "prompt after load")
    if m.group(1).startswith("ERROR"):
        msg = f"{g.name}: {m.group(0)}"
        raise boot.BootError(msg)


def reconnect(g: boot.Guest) -> float | None:
    """`reconnect -r`; the seconds it took, or None if it never returned."""
    start = time.monotonic()
    g.send("reconnect -r\r")
    try:
        g.expect(PROMPT, "prompt after reconnect -r", BOUND)
    except boot.BootError:
        return None
    return time.monotonic() - start


def run_tcp(g: boot.Guest, name: str, port: int, server: bool) -> int:
    """reconnect -r with an accepted connection held; 0 on GREEN."""
    if not server:
        g.command("ifconfig -s eth0 static 10.0.2.15 255.255.255.0 10.0.2.2")
        took = reconnect(g)
        say(f"{name}: CONTROL reconnect -r with no server: " + ("hung" if took is None else f"{took:.1f} s"))
        return 1 if took is None else 0
    load_tcphold(g)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(b"hello\n")
        g.expect(re.compile(r"tcphold: accepted"), "tcphold accepted", 30)
        g.expect(re.compile(r"tcphold: received 6 bytes"), "tcphold received", 30)
        before = tcphold_sockets(g)
        took = reconnect(g)
        if took is None:
            say(f"{name}: RED-HANG reconnect -r with an accepted connection held, sockets {before}")
            return 1
        after = tcphold_sockets(g)
    time.sleep(3)
    load_tcphold(g)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as again:
        again.sendall(b"again\n")
        g.expect(re.compile(r"tcphold: received 6 bytes"), "a connection after the reconnect", 30)
    ok = len(before) == 2 and not after
    verdict = "GREEN" if ok else "NOT-GREEN"
    say(f"{name}: {verdict} reconnect -r {took:.1f} s; sockets {before} before, {after} after; a new server accepted")
    return 0 if ok else 1


def main() -> int:
    """Parse the arguments, boot, run the mode."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("vfs", "tcp", "idle"))
    ap.add_argument("arch", choices=("x64", "aarch64"))
    ap.add_argument("code", type=Path)
    ap.add_argument("vars", type=Path)
    ap.add_argument("--shell", type=Path, required=True)
    ap.add_argument("--apps", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--qemu-dir", type=Path)
    ap.add_argument("--virtiofsd", default=shutil.which("virtiofsd") or "/usr/libexec/virtiofsd")
    ap.add_argument("--cpu", default=boot.AARCH64_CPU, help="AARCH64's CPU model")
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--seconds", type=float, default=20.0)
    a = ap.parse_args()
    mode: str = a.mode
    arch: str = a.arch
    out: Path = a.out
    apps: Path = a.apps
    name: str = a.name or f"{arch}-{mode}"
    out.mkdir(parents=True, exist_ok=True)
    s = boot.ImageSet(arch, "DEBUG", a.code, a.vars, a.shell, "edk2-stable202611")
    with tempfile.TemporaryDirectory(dir=out) as tmp, tempfile.TemporaryDirectory(prefix="axlfw-r-") as socks:
        t = Path(tmp)
        esp = t / "esp"
        (esp / "EFI/BOOT").mkdir(parents=True)
        shutil.copyfile(a.shell, esp / "EFI/BOOT" / ("BOOTX64.EFI" if arch == "x64" else "BOOTAA64.EFI"))
        shutil.copyfile(apps / "TcpHold.efi", esp / "TcpHold.efi")
        vars_copy = t / "vars.fd"
        shutil.copyfile(a.vars, vars_copy)
        argv = boot.qemu_argv(a.qemu_dir, s, vars_copy, esp, a.cpu)
        fsd: subprocess.Popen[bytes] | None = None
        port = free_port()
        if mode == "vfs":
            share = t / "share"
            share.mkdir()
            shutil.copyfile(apps / "VfsRace.efi", share / "VfsRace.efi")
            (share / "race.txt").write_bytes(bytes((i * 7 + 3) % 251 for i in range(3000)))
            sock = Path(socks) / "fs.sock"
            # --cache=never would drop FUSE readdirplus, which VirtioFsDxe needs.
            fsd = subprocess.Popen(  # noqa: S603 - argv is ours
                [a.virtiofsd, f"--socket-path={sock}", f"--shared-dir={share}", "--sandbox=none", "--cache=auto"],
                stdout=subprocess.DEVNULL,
                stderr=(out / f"{name}.virtiofsd.log").open("wb"),
            )
            for _ in range(100):
                if sock.exists():
                    break
                time.sleep(0.05)
            argv += [
                "-object", "memory-backend-memfd,id=mem,size=1024M,share=on",
                "-numa", "node,memdev=mem",
                "-chardev", f"socket,id=vfs,path={sock}",
                "-device", "vhost-user-fs-pci,queue-size=1024,chardev=vfs,tag=share",
            ]  # fmt: skip
        elif mode == "tcp":
            # DxeNetLib needs an RNG: cortex-a57 has none (and see DESIGN
            # §5 on reconnect -r with only virtio-rng on AARCH64).
            argv += [
                "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:5555",
                "-device", "virtio-net-pci,netdev=n0,romfile=",
                "-device", "virtio-rng-pci",
            ]  # fmt: skip
        if arch == "x64":
            argv += ["-debugcon", f"file:{out / (name + '.debugcon')}", "-global", "isa-debugcon.iobase=0x402"]
        else:
            argv += ["-serial", f"file:{out / (name + '.uart1')}"]
        g = boot.Guest(argv, out, Path(socks), name, BOUND * 3)
        rc = 1
        try:
            g.prompt()
            if mode == "vfs":
                rc = run_vfs(g, name)
            elif mode == "tcp":
                rc = run_tcp(g, name, port, not a.no_server)
            else:
                rc = run_idle(g, name, a.seconds)
        except boot.BootError as err:
            say(f"{name}: BOOTERROR {err}")
        finally:
            if rc != 0:
                evidence(g, out, name, arch)
            g.close()
            if fsd:
                fsd.terminate()
                fsd.wait(10)
    return rc


if __name__ == "__main__":
    sys.exit(main())
