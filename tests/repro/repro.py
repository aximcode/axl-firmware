"""Reproducers of r3's patches 0011 (VirtioFsDxe), 0012 (LocalApic EOI)
and 0013 (TcpDxe), run by hand against any image (DESIGN §4.30-4.32).
vfs and tcp are the release's own boot checks (lib/axlfw/netfs.py,
DESIGN §4.38), run here on one image with the evidence of a failure.

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
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from axlfw import boot, netfs  # noqa: E402

BOUND = netfs.BOUND
hmp_reply = boot._hmp_reply  # pyright: ignore[reportPrivateUsage]


def say(msg: str) -> None:
    """One line of the run's record."""
    print(msg, flush=True)


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
    """VfsRace from the share, then reconnect -r; 0 on PASS."""
    try:
        line, took = netfs.vfs_race(g)
    except boot.BootError as err:
        say(f"{name}: RED {str(err).splitlines()[0]}")
        return 1
    say(f"{name}: {line} after {took:.1f} s; after reconnect -r the share lists race.txt again")
    return 0


def run_tcp(g: boot.Guest, name: str, port: int, server: bool) -> int:
    """reconnect -r with an accepted connection held; 0 on GREEN."""
    if not server:
        g.command("ifconfig -s eth0 static 10.0.2.15 255.255.255.0 10.0.2.2")
        took = netfs.reconnect(g)
        say(f"{name}: CONTROL reconnect -r with no server: " + ("hung" if took is None else f"{took:.1f} s"))
        return 1 if took is None else 0
    try:
        took, before = netfs.tcp_reconnect(g, port)
    except boot.BootError as err:
        say(f"{name}: RED {str(err).splitlines()[0]}")
        return 1
    say(f"{name}: GREEN reconnect -r {took:.1f} s; sockets {before} before, none after; a new server accepted")
    return 0


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
    ap.add_argument("--virtiofsd", type=Path, default=netfs.virtiofsd())
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
        esp, vars_copy = netfs.esp_with(s, t, [apps / netfs.TCP_APP])
        fsd: subprocess.Popen[bytes] | None = None
        port = netfs.free_port()
        if mode == "vfs":
            if a.virtiofsd is None:
                say(f"{name}: SETUP no virtiofsd (--virtiofsd, {netfs.VIRTIOFSD_ENV}, PATH or /usr/libexec)")
                return 2
            share = t / "share"
            share.mkdir()
            shutil.copyfile(apps / netfs.VFS_APP, share / netfs.VFS_APP)
            (share / netfs.RACE_FILE).write_bytes(netfs.RACE_BYTES)
            sock = Path(socks) / "fs.sock"
            try:
                fsd = netfs.start_virtiofsd(a.virtiofsd, share, sock, out / f"{name}.virtiofsd.log")
            except boot.BootError as err:
                say(f"{name}: SETUP {err}")
                return 2
            argv = netfs.vfs_argv(a.qemu_dir, s, vars_copy, esp, sock)
        elif mode == "tcp":
            argv = netfs.tcp_argv(a.qemu_dir, s, vars_copy, esp, port, a.cpu)
        else:
            argv = boot.qemu_argv(a.qemu_dir, s, vars_copy, esp, a.cpu)
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
                netfs.stop(fsd)
    return rc


if __name__ == "__main__":
    sys.exit(main())
