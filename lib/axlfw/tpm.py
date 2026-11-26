"""The TPM boot check: each image set booted with a TPM attached.

QEMU's TPM is swtpm's, over its control socket: `tpm-tis` on X64,
`tpm-tis-device` on AARCH64 (on virt's platform bus, which PEI maps only
by patch 0014). Each image set must reach the Shell with it; given
tests/tpm-pcr/'s application, PcrRead.efi (build.sh builds it for each arch,
out of tree), the Shell runs it, and it must read PCRs 0-7 of the SHA-256
bank with TPM2_PCR_Read through EFI_TCG2_PROTOCOL:

- all eight, in order, and its closing line;
- PCR0 neither zero nor only the separator: the firmware measured itself;
- PCRs 3 and 6 exactly the separator's extend alone, which Tcg2Dxe makes in
  each of PCRs 0-7 at ReadyToBoot: the Shell was booted with measurement.

swtpm must be on PATH: a missing one is a problem, never a skip. `build.sh
check` on a packed release has no application, so there the boot runs to
the Shell only.

docs/DESIGN.md §4.37.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from axlfw import boot

APP = "PcrRead.efi"
DEVICE = {"x64": "tpm-tis", "aarch64": "tpm-tis-device"}
PCRS = 8
# A SHA-256 PCR extended with EV_SEPARATOR's digest (of four zero bytes)
# and nothing else.
SEPARATOR_ONLY = hashlib.sha256(bytes(32) + hashlib.sha256(bytes(4)).digest()).hexdigest()
# The PCRs that hold the separator alone on QEMU: no option ROM, no
# platform-config or host-platform measurement in either firmware.
SEPARATOR_PCRS = (3, 6)
DONE = "axlfw-pcr: done"
# PrintLib's %x is upper-case hex; read in either case, kept in lower.
_PCR = re.compile(r"^axlfw-pcr: sha256 PCR(\d+) ([0-9A-Fa-f]+)\r?$", re.MULTILINE)
_ERROR = re.compile(r"^axlfw-pcr: error: .*?\r?$", re.MULTILINE)
# How long swtpm has to make its control socket.
SWTPM_START = 10.0


def app(ws: Path, toolchain: str, arch: str) -> Path:
    """Where build.sh's build of PcrRead.efi for arch is."""
    return ws / "Build" / "AxlFwTpm" / f"DEBUG_{toolchain}" / arch.upper() / APP


def missing_swtpm() -> list[str]:
    """swtpm, if it cannot be found, said."""
    return [] if shutil.which("swtpm") else ["swtpm not found on PATH: the TPM boot check needs it"]


def pcr_problems(out: str) -> tuple[dict[int, str], list[str]]:
    """PcrRead.efi's output: the PCRs it printed, and what is wrong with them."""
    found = [(int(n), v.lower()) for n, v in _PCR.findall(out)]
    pcrs = dict(found)
    problems = [m.group(0).rstrip("\r") for m in _ERROR.finditer(out)]
    order = [n for n, _ in found]
    if order != list(range(PCRS)):
        problems.append(f"PCRs printed: {order}, not 0-{PCRS - 1} in order")
    problems += [f"PCR{n} is {v}, not 64 hex digits" for n, v in sorted(pcrs.items()) if len(v) != 64]
    if DONE not in out:
        problems.append(f"no {DONE!r} line")
    if pcrs.get(0) in ("0" * 64, SEPARATOR_ONLY):
        problems.append(f"PCR0 is {pcrs[0]}: the firmware was not measured")
    problems += [
        f"PCR{n} is {pcrs.get(n)}, not the separator alone ({SEPARATOR_ONLY})"
        for n in SEPARATOR_PCRS
        if pcrs.get(n) != SEPARATOR_ONLY
    ]
    return pcrs, problems


def _start_swtpm(state: Path, sock: Path, err: Path) -> tuple[subprocess.Popen[bytes] | None, str]:
    """swtpm for one boot, once its control socket is there; else why not."""
    with err.open("wb") as stderr:
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is ours
                [
                    "swtpm",
                    "socket",
                    "--tpm2",
                    "--tpmstate",
                    f"dir={state}",
                    "--ctrl",
                    f"type=unixio,path={sock}",
                    "--terminate",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        except OSError as e:
            return None, f"cannot start swtpm: {e.strerror}"
    deadline = time.monotonic() + SWTPM_START
    while not sock.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            _stop(proc, kill=True)
            return None, f"swtpm made no control socket ({err}: {err.read_text(errors='replace').strip()!r})"
        time.sleep(0.05)
    return proc, ""


def _stop(proc: subprocess.Popen[bytes], kill: bool = False) -> None:
    """swtpm ended: killed, or given the time to end itself (it does when
    QEMU closes its control channel, --terminate), then killed."""
    if not kill:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def check(s: boot.ImageSet, pcr_app: Path | None, qemu_dir: Path | None, workdir: Path, timeout: float) -> boot.Result:
    """Boot s with a TPM: the Shell, and with pcr_app, PCRs 0-7 read.

    swtpm and pcr_app are the caller's to have checked (missing_swtpm;
    release.boot_checks refuses both before any boot).

    The result's facts: {"tpm": {"device", "shell", "pcrs_read"}}.
    """
    result = boot.Result()
    workdir.mkdir(parents=True, exist_ok=True)
    tag = f"{s.arch}-{s.flavour.lower()}-tpm"
    fact: dict[str, object] = {"device": DEVICE[s.arch], "shell": False, "pcrs_read": False}
    result.facts["tpm"] = fact
    with tempfile.TemporaryDirectory(dir=workdir) as tmp, tempfile.TemporaryDirectory(prefix="axlfw-") as socks:
        t = Path(tmp)
        esp = t / "esp"
        boot_dir = esp / "EFI" / "BOOT"
        boot_dir.mkdir(parents=True)
        shutil.copyfile(s.shell, boot_dir / ("BOOTX64.EFI" if s.arch == "x64" else "BOOTAA64.EFI"))
        if pcr_app is not None:
            shutil.copyfile(pcr_app, esp / APP)
        vars_copy = t / "vars.fd"
        shutil.copyfile(s.vars, vars_copy)
        state = t / "tpm"
        state.mkdir()
        sock = Path(socks) / "swtpm.ctrl"
        swtpm, why = _start_swtpm(state, sock, workdir / f"{tag}-swtpm.stderr")
        if swtpm is None:
            result.problems.append(f"{s.arch} {s.flavour}: {why}")
            return result
        argv = [
            *boot.qemu_argv(qemu_dir, s, vars_copy, esp),
            "-chardev",
            f"socket,id=chrtpm,path={sock}",
            "-tpmdev",
            "emulator,id=tpm0,chardev=chrtpm",
            "-device",
            f"{DEVICE[s.arch]},tpmdev=tpm0",
        ]
        # Whatever happens from here, swtpm is stopped: killed at once when
        # no QEMU started, else once QEMU is closed.
        started = False
        try:
            g = boot.Guest(argv, workdir, Path(socks), tag, timeout)
            started = True
            try:
                g.prompt()
                fact["shell"] = True
                if pcr_app is not None:
                    _, more = pcr_problems(g.command(f"fs0:\\{APP}"))
                    fact["pcrs_read"] = not more
                    result.problems += [f"{s.arch} {s.flavour}: with a TPM, {APP}: {p}" for p in more]
            except boot.BootError as err:
                result.problems.append(f"{s.arch} {s.flavour}: with a TPM ({DEVICE[s.arch]}): {err}")
            finally:
                g.close()
        except boot.BootError as err:
            result.problems.append(f"{s.arch} {s.flavour}: {err}")
        finally:
            _stop(swtpm, kill=not started)
    return result
