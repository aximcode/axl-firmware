"""The r3 console reproducers: each boots one image set of an unpacked
release in QEMU and says what it observed, as whole values, and whether
that is the fixed behaviour.

    repro.py --release DIR --probe DIR --out DIR [--qemu-dir DIR]
             [--flavour DEBUG|RELEASE] ITEM:ARCH...

DIR for --release is an unpacked or installed release (r2's install, or a
build's work/stage/axl-firmware-<release>); --probe the build directory of
AxlConsoleReproPkg (Build/AxlConsoleRepro/RELEASE_GCC, holding X64/ and
AARCH64/Probe.efi, built by build-probe.sh beside this file). The items (DESIGN §4.26, §4.28, §4.29):

    ps2-mouse        x64: a SimplePointer at Acpi(PNP0F13,0x0), and three
                     `mouse_move 10 5` reaching it (patch 0006)
    ctrl-s           a serial Ctrl-S pauses the ticks script, a key resumes
                     it (patch 0007)
    widechar         VT100: U+0141 U+0158 written as '??', not 'AX' (0008)
    delete-absent    DeleteFileByName on a virtiofs share's absent file:
                     EFI_NOT_FOUND, and nothing created (patch 0009)
    wheel            virtio-mouse-pci: ResolutionZ 8, and two wheel steps up
                     and one down giving RelativeMovementZ +1 (patch 0010)
    pci-serial-bootindex
                     aarch64: with bootindex=0 on the boot disk, two
                     SerialIo handles before any connect (patch 0005)
    del-backspace    `echo abX`, DEL, `c` prints abc (AARCH64 VT-UTF8)

Exit status 0 when every item showed the fixed behaviour, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from axlfw import boot, install  # noqa: E402

VIRTIOFSD = Path("/usr/libexec/virtiofsd")
_POINTER = re.compile(r"pointer (\d+): (\S+) res (\d+),(\d+),(\d+)")
_PROMPT = re.compile(r"(?:Shell|FS\d+:[^>\n]*)> ")
_STATE = re.compile(r"pointer (\d+): x=(-?\d+) y=(-?\d+) z=(-?\d+) l=(\d) r=(\d)")


class Run:
    """One boot: its guest, and what the item observed."""

    def __init__(self, g: boot.Guest) -> None:
        """Wrap a started guest."""
        self.g = g
        self.observed: list[str] = []
        self.fixed = False

    def say(self, line: str) -> None:
        """Record an observation."""
        self.observed.append(line)


def pointers(g: boot.Guest, seconds: int, moves: list[str]) -> tuple[dict[int, tuple[str, int]], dict[int, list[int]]]:
    """Run `probe pointer`, send each HMP mouse command once it is ready.

    Returns each pointer's path and ResolutionZ, and its summed x, y, z.
    """
    g.send(f"fs0:\\probe.efi pointer {seconds}\r")
    start = g.mark
    m = g.expect(re.compile(r"pointer ready"), "probe's pointer list")
    listed = {int(i): (path, int(rz)) for i, path, _, _, rz in _POINTER.findall(g.output(start, m.start()))}
    for line in moves:
        g.monitor(line)
        time.sleep(0.3)
    start = g.mark
    m = g.expect(re.compile(r"pointer done"), "probe's pointer states", seconds + 30)
    sums: dict[int, list[int]] = {}
    for i, x, y, z, _, _ in _STATE.findall(g.output(start, m.start())):
        acc = sums.setdefault(int(i), [0, 0, 0])
        acc[0] += int(x)
        acc[1] += int(y)
        acc[2] += int(z)
    g.expect(_PROMPT, "prompt after probe")
    return listed, sums


def mice(g: boot.Guest) -> str:
    """HMP `info mice`, as one line."""
    reply = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", g.monitor("info mice")).replace("\r", "")
    return " | ".join(x.strip() for x in reply.splitlines() if "Mouse #" in x)


def ps2_mouse(r: Run) -> None:
    """Patch 0006: the PS/2 mouse has a SimplePointer, and moves."""
    r.g.prompt()
    r.say(f"info mice: {mice(r.g)}")
    listed, sums = pointers(r.g, 5, ["mouse_move 10 5"] * 3)
    for i, (path, rz) in sorted(listed.items()):
        r.say(f"pointer {i}: {path} ResolutionZ {rz}, moved x,y,z {sums.get(i, [0, 0, 0])}")
    ps2 = [i for i, (path, _) in listed.items() if path.endswith("/Acpi(PNP0F13,0x0)")]
    if not listed:
        r.say("no SimplePointer with a device path")
    r.fixed = len(ps2) == 1 and sums.get(ps2[0], [0, 0, 0])[0] == 30


def wheel(r: Run) -> None:
    """Patch 0010: the virtio mouse's wheel reaches RelativeMovementZ."""
    r.g.prompt()
    info = mice(r.g)
    m = re.search(r"Mouse #(\d+): QEMU Virtio Mouse", info)
    if m:
        r.g.monitor(f"mouse_set {m.group(1)}")
    r.say(f"info mice: {mice(r.g)}")
    listed, sums = pointers(r.g, 5, ["mouse_move 0 0 1", "mouse_move 0 0 1", "mouse_move 0 0 -1", "mouse_move 4 0"])
    virtio = [i for i, (path, _) in listed.items() if "Acpi(" not in path]
    for i, (path, rz) in sorted(listed.items()):
        r.say(f"pointer {i}: {path} ResolutionZ {rz}, moved x,y,z {sums.get(i, [0, 0, 0])}")
    r.fixed = len(virtio) == 1 and listed[virtio[0]][1] == 8 and sums.get(virtio[0]) == [4, 0, 1]


def ctrl_s(r: Run) -> None:
    """Patch 0007: a serial Ctrl-S pauses the ticks, a key resumes them."""
    r.g.prompt()
    try:
        got = boot.ctrl_s_pause(r.g)
    except boot.BootError as err:
        r.say(str(err).split("\n")[0])
        return
    r.say(f"paused after tick {got['paused_after']}, resumed with tick {got['resumed_with']}")
    r.fixed = True


def del_backspace(r: Run) -> None:
    """VT-UTF8: DEL is Backspace."""
    r.g.prompt()
    try:
        boot.del_backspace(r.g)
    except boot.BootError as err:
        r.say(str(err))
        return
    r.say("`echo abX`, DEL, `c` printed 'abc'")
    r.fixed = True


def widechar(r: Run) -> None:
    """Patch 0008: on VT100, characters above 0x7F are written as '?'."""
    r.g.prompt()
    out = r.g.command("fs0:\\probe.efi wide")
    for line in out.splitlines():
        if line.startswith(("vt100 ", "wide")):
            r.say(line.strip())
    m = re.search(r"wide<(.*?)>", out)
    r.fixed = m is not None and m.group(1) == "??"


def pci_serial(r: Run) -> None:
    """Patch 0005 (amended): a PCI 16550 has its SerialIo with a QEMU boot order."""
    r.g.prompt()
    out = r.g.command("dh -p SerialIo -v")
    paths = re.findall(r"(?:VenHw|PciRoot)\([^\s]*Uart\([^)]*\)", out)
    r.say(f"dh -p SerialIo: {len(paths)} handles: {', '.join(paths)}")
    r.fixed = len(paths) == 2


class Inotify:
    """The directory events a virtiofs share's host sees (Linux inotify)."""

    IN_CREATE = 0x100
    IN_DELETE = 0x200
    IN_ISDIR = 0x40000000

    def __init__(self, path: Path) -> None:
        """Watch path for files and directories made and removed."""
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK)
        if self.fd < 0 or libc.inotify_add_watch(self.fd, str(path).encode(), self.IN_CREATE | self.IN_DELETE) < 0:
            msg = f"inotify on {path}: errno {ctypes.get_errno()}"
            raise OSError(msg)

    def events(self) -> list[str]:
        """The events since the last call, as 'create NAME', 'delete dir NAME'."""
        out: list[str] = []
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return out
        at = 0
        while at < len(data):
            _, mask, _, size = struct.unpack_from("iIII", data, at)
            name = data[at + 16 : at + 16 + size].rstrip(b"\0").decode()
            kind = "create" if mask & self.IN_CREATE else "delete"
            out.append(f"{kind}{' dir' if mask & self.IN_ISDIR else ''} {name}")
            at += 16 + size
        return out


def delete_absent(r: Run, share: Path) -> None:
    """Patch 0009: DeleteFileByName of an absent file creates nothing."""
    r.g.prompt()
    # BDS does not connect the share's device: connect it, and map it.
    r.g.command("connect -r")
    r.g.command("map -r")
    fs = ""
    for n in range(4):
        if "marker.txt" in r.g.command(f"ls fs{n}:\\marker.txt"):
            fs = f"fs{n}"
            break
    if not fs:
        r.say("no file system holds the share's marker.txt")
        return
    watch = Inotify(share)
    results: list[str] = []
    for name in ("absent.txt", "newdir\\absent.txt"):
        out = r.g.command(f"{fs}:\\probe.efi delete {fs}:\\{name}")
        time.sleep(0.5)
        lines = [x.strip() for x in out.splitlines() if x.startswith(("delete ", "exists after"))]
        events = watch.events()
        r.say(f"{' / '.join(lines)}; host inotify: {events or 'nothing'}")
        results.append(lines[0].split(": ", 1)[1] if lines else "")
    left = sorted(p.name for p in share.iterdir())
    r.say(f"share afterwards: {left}")
    r.fixed = all(x.startswith("Not Found") for x in results) and left == ["marker.txt", "probe.efi"]


ITEMS: dict[str, tuple[tuple[str, ...], Callable[..., None]]] = {
    "ps2-mouse": (("x64",), ps2_mouse),
    "ctrl-s": (("x64", "aarch64"), ctrl_s),
    "widechar": (("x64", "aarch64"), widechar),
    "delete-absent": (("x64", "aarch64"), delete_absent),
    "wheel": (("x64", "aarch64"), wheel),
    "pci-serial-bootindex": (("aarch64",), pci_serial),
    "pci-serial-no-bootindex": (("aarch64",), pci_serial),
    "del-backspace": (("x64", "aarch64"), del_backspace),
}


def run_item(item: str, arch: str, a: argparse.Namespace) -> Run | str:
    """Boot for one item; its Run, or why it could not run."""
    d = a.release / (arch if a.flavour == "DEBUG" else f"release/{arch}")
    files = install.image_files(arch)
    s = boot.ImageSet(arch, a.flavour, d / "code.fd", d / "vars.fd", d / Path(files["shell"]).name, "")
    probe = a.probe / ("X64" if arch == "x64" else "AARCH64") / "Probe.efi"
    tag = f"{item}-{arch}-{a.flavour.lower()}"
    with tempfile.TemporaryDirectory(dir=a.out) as tmp, tempfile.TemporaryDirectory(prefix="axlrepro-") as socks:
        t = Path(tmp)
        esp = t / "esp"
        (esp / "EFI" / "BOOT").mkdir(parents=True)
        shutil.copyfile(s.shell, esp / "EFI" / "BOOT" / ("BOOTX64.EFI" if arch == "x64" else "BOOTAA64.EFI"))
        shutil.copyfile(probe, esp / "probe.efi")
        (esp / boot.TICKS_SCRIPT).write_bytes(boot.TICKS.encode())
        vars_copy = t / "vars.fd"
        shutil.copyfile(s.vars, vars_copy)
        argv = boot.qemu_argv(a.qemu_dir, s, vars_copy, esp)
        extra: list[str] = []
        fsd: subprocess.Popen[bytes] | None = None
        share = t / "share"
        if item == "wheel":
            extra = ["-device", "virtio-mouse-pci"]
        elif item.startswith("pci-serial"):
            extra = ["-chardev", "null,id=pciser", "-device", "pci-serial,chardev=pciser"]
            if item == "pci-serial-bootindex":
                argv = [x + ",bootindex=0" if x == "virtio-blk-pci,drive=esp" else x for x in argv]
        elif item == "delete-absent":
            share.mkdir()
            (share / "marker.txt").write_text("marker\n")
            shutil.copyfile(probe, share / "probe.efi")
            sock = Path(socks) / "vfs.sock"
            fsd = subprocess.Popen(  # noqa: S603 - our own arguments
                [str(VIRTIOFSD), f"--socket-path={sock}", f"--shared-dir={share}", "--sandbox=none"],
                stdout=subprocess.DEVNULL,
                stderr=(a.out / f"{tag}.virtiofsd").open("wb"),
            )
            for _ in range(100):
                if sock.exists():
                    break
                time.sleep(0.05)
            extra = [
                "-object", "memory-backend-file,id=mem,size=1024M,mem-path=/dev/shm,share=on",
                "-numa", "node,memdev=mem",
                "-chardev", f"socket,id=vfs,path={sock}",
                "-device", "vhost-user-fs-pci,queue-size=1024,chardev=vfs,tag=share",
            ]  # fmt: skip
        try:
            g = boot.Guest([*argv, *extra], a.out, Path(socks), tag, 180)
        except boot.BootError as err:
            return str(err)
        r = Run(g)
        try:
            _, body = ITEMS[item]
            if item == "delete-absent":
                body(r, share)
            else:
                body(r)
        except boot.BootError as err:
            r.say(f"BootError: {str(err).splitlines()[0]}")
        finally:
            g.close()
            if fsd is not None:
                fsd.terminate()
                fsd.wait(10)
        return r


def main() -> int:
    """Run each ITEM:ARCH given; say what each observed."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--release", type=Path, required=True)
    p.add_argument("--probe", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--qemu-dir", type=Path)
    p.add_argument("--flavour", choices=("DEBUG", "RELEASE"), default="DEBUG")
    p.add_argument("items", nargs="+")
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    every = True
    for spec in a.items:
        item, _, arch = spec.partition(":")
        if item not in ITEMS or arch not in ITEMS[item][0]:
            print(f"{spec}: no such item (or not on that arch)", file=sys.stderr)
            return 2
        r = run_item(item, arch, a)
        if isinstance(r, str):
            print(f"{spec} {a.flavour}: did not run: {r}")
            every = False
            continue
        for line in r.observed:
            print(f"{spec} {a.flavour}: {line}")
        print(f"{spec} {a.flavour}: {'FIXED' if r.fixed else 'NOT FIXED'}", flush=True)
        every = every and r.fixed
    return 0 if every else 1


if __name__ == "__main__":
    sys.exit(main())
