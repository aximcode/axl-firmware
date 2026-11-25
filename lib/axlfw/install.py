"""Install a release: verify its sha256, unpack it, write its descriptors.

    <prefix>/<release>/...               the unpacked release
    <prefix>/descriptors/<name>.json     its QEMU firmware descriptors, with
                                         absolute paths

<prefix> is $XDG_DATA_HOME/axl-firmware by default. The release comes from
a local tarball (its sha256 from --sha256, or from SHA256SUMS beside it) or
by release tag from GitHub (`gh release download`, or the REST API with
GITHUB_TOKEN; the repository is private), its sha256 from the release's
SHA256SUMS or --sha256.

Every file is written beside its place and renamed in. The release's
directory is unpacked whole, checked against its manifest, then renamed in;
the descriptors are written last, once what they name is in place, and
then every other descriptor of ours (an earlier release's, which would sort
first) is removed. Needs only Python's standard library (3.9 or later).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import cast

_TAG = re.compile(r"^edk2-stable\d{6}(?:\.\d+)?-r\d+$")
_OURS = re.compile(r"^[12]0-axl-firmware-.*\.json$")
FORMAT = 2


class InstallError(Exception):
    """The install stopped; nothing it would have replaced was touched."""


def default_prefix() -> Path:
    """$XDG_DATA_HOME/axl-firmware; a relative XDG_DATA_HOME is ignored."""
    data = os.environ.get("XDG_DATA_HOME", "")
    base = Path(data) if data.startswith("/") else Path.home() / ".local" / "share"
    return base / "axl-firmware"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sums(path: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([0-9a-f]{64})  \*?(\S+)$", line)
        if m:
            sums[m.group(2)] = m.group(1)
    return sums


def _api(url: str, token: str, accept: str) -> urllib.request.Request:
    req = urllib.request.Request(  # noqa: S310 - https only, below
        url, headers={"Accept": accept, "User-Agent": "axl-firmware-install"}
    )
    # Unredirected: an asset redirects to a storage host, which must not
    # receive the token.
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    return req


def download(repo: str, tag: str, names: list[str], dest: Path) -> None:
    """Fetch the named assets of release tag into dest."""
    gh = shutil.which("gh")
    if gh:
        cmd = [gh, "release", "download", tag, "-R", repo, "-D", str(dest)]
        for n in names:
            cmd += ["-p", n]
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if r.returncode == 0:
            return
        if not os.environ.get("GITHUB_TOKEN"):
            msg = f"gh release download {tag} -R {repo} failed: {r.stderr.strip()}"
            raise InstallError(msg)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        msg = f"cannot fetch {repo} {tag}: install gh (and `gh auth login`) or set GITHUB_TOKEN"
        raise InstallError(msg)
    with urllib.request.urlopen(  # noqa: S310
        _api(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token, "application/vnd.github+json"),
        timeout=60,
    ) as resp:
        info = cast("dict[str, object]", json.load(resp))
    assets = {str(a["name"]): str(a["url"]) for a in cast("list[dict[str, object]]", info.get("assets", []))}
    for n in names:
        if n not in assets:
            msg = f"{repo} {tag} has no asset {n}"
            raise InstallError(msg)
        with urllib.request.urlopen(_api(assets[n], token, "application/octet-stream"), timeout=600) as resp:  # noqa: S310
            with (dest / n).open("wb") as out:
                shutil.copyfileobj(resp, out)


def _verify(path: Path, sha: str | None) -> None:
    want = sha
    if want is None:
        sums = path.parent / "SHA256SUMS"
        if not sums.is_file():
            msg = f"{path}: no SHA256SUMS beside it; give its sha256 with --sha256"
            raise InstallError(msg)
        want = _sums(sums).get(path.name)
        if want is None:
            msg = f"{sums} does not list {path.name}"
            raise InstallError(msg)
    got = _sha256(path)
    if got != want.lower():
        msg = f"{path}: sha256 {got}, expected {want}"
        raise InstallError(msg)


def _extract(tarball: Path, top: str, into: Path) -> Path:
    """Unpack tarball, whose every member must sit under top/, into into."""
    with tarfile.open(tarball, "r:xz") as tar:
        members = tar.getmembers()
        for m in members:
            parts = Path(m.name).parts
            if (
                not parts
                or parts[0] != top
                or ".." in parts
                or m.name.startswith("/")
                or not (m.isfile() or m.isdir())
            ):
                msg = f"{tarball}: unexpected member {m.name!r}"
                raise InstallError(msg)
        if hasattr(tarfile, "data_filter"):
            tar.extractall(into, members, filter="data")
        else:  # Python before 3.12: the members are checked above.
            tar.extractall(into, members)  # noqa: S202
    return into / top


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _inside(root: Path, rel: str) -> Path:
    """root/rel, which must name something under root."""
    p = (root / rel).resolve()
    if rel.startswith("/") or root.resolve() not in p.parents:
        msg = f"{root}: the manifest names {rel!r}, outside the release"
        raise InstallError(msg)
    return p


def _check_set(root: Path, release: str) -> dict[str, object]:
    manifest = cast("dict[str, object]", json.loads((root / "manifest.json").read_text()))
    if manifest.get("format") != FORMAT:
        msg = f"{root}: manifest format {manifest.get('format')!r}; this install reads format {FORMAT}"
        raise InstallError(msg)
    if manifest.get("release") != release:
        msg = f"{root}: its manifest names release {manifest.get('release')!r}, not {release!r}"
        raise InstallError(msg)
    for d, entry in cast("dict[str, dict[str, object]]", manifest["images"]).items():
        for key in ("code", "vars", "shell"):
            f = cast("dict[str, object]", entry[key])
            p = _inside(root, str(f["path"]))
            if not p.is_file() or p.stat().st_size != f["size"] or _sha256(p) != f["sha256"]:
                msg = f"{p}: not {d}'s {key} as its manifest records it"
                raise InstallError(msg)
    return manifest


def _descriptors(unpacked: Path, dest: Path, manifest: dict[str, object]) -> list[tuple[str, str]]:
    """Each descriptor of the release, its paths made absolute under dest.

    Checked against the unpacked release before anything is replaced.
    """
    out: list[tuple[str, str]] = []
    for rel in cast("list[str]", manifest["descriptors"]):
        name = Path(rel).name
        if rel != f"descriptors/{name}" or not _OURS.match(name):
            msg = f"{unpacked}: the manifest lists a descriptor {rel!r}, not descriptors/[12]0-axl-firmware-*.json"
            raise InstallError(msg)
        d = cast("dict[str, object]", json.loads((unpacked / rel).read_text()))
        mapping = cast("dict[str, dict[str, str]]", d["mapping"])
        for key in ("executable", "nvram-template"):
            f = mapping[key]["filename"]
            if not _inside(unpacked, f).is_file():
                msg = f"{unpacked / rel}: names {f}, which is not in the release"
                raise InstallError(msg)
            mapping[key]["filename"] = str(dest / f)
        out.append((name, json.dumps(d, indent=2) + "\n"))
    return out


def install(repo: str, source: str, prefix: Path | None, sha: str | None, symbols: bool) -> None:
    """Install a release from a tarball path or a release tag.

    One install at a time per prefix (a lock on <prefix>/.lock). The
    release and its descriptors are checked before anything is replaced; a
    release directory already there is kept until the new descriptors are
    written, and put back if writing them fails.
    """
    prefix = (prefix or default_prefix()).resolve()
    prefix.mkdir(parents=True, exist_ok=True)
    with (prefix / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _install(repo, source, prefix, sha, symbols)


def _install(repo: str, source: str, prefix: Path, sha: str | None, symbols: bool) -> None:
    with tempfile.TemporaryDirectory(dir=prefix, prefix=".install.") as tmpname:
        tmp = Path(tmpname)
        path = Path(source)
        if path.is_file():
            m = re.match(r"^axl-firmware-(.+)\.tar\.xz$", path.name)
            if not m or not _TAG.match(m.group(1)):
                msg = f"{path}: not named axl-firmware-<release>.tar.xz, with a release edk2-stableYYYYMM-rN"
                raise InstallError(msg)
            release = m.group(1)
        elif _TAG.match(source):
            release = source
            names = [f"axl-firmware-{release}.tar.xz", "SHA256SUMS"]
            if symbols:
                names += [f"axl-firmware-{release}-symbols-{a}.tar.xz" for a in ("x64", "aarch64")]
            fetched = tmp / "fetched"
            fetched.mkdir()
            print(f"fetching {repo} {release}")
            download(repo, release, names, fetched)
            path = fetched / names[0]
        else:
            msg = f"--from {source}: no such file, and not a release tag (edk2-stableYYYYMM-rN)"
            raise InstallError(msg)
        _verify(path, sha)
        print(f"{path.name}: sha256 verified")
        top = f"axl-firmware-{release}"
        unpacked = _extract(path, top, tmp)
        manifest = _check_set(unpacked, release)
        if symbols:
            for arch in ("x64", "aarch64"):
                sym = path.parent / f"axl-firmware-{release}-symbols-{arch}.tar.xz"
                _verify(sym, None)
                part = _extract(sym, f"{top}-symbols-{arch}", tmp)
                (unpacked / "symbols").mkdir(exist_ok=True)
                os.replace(part / arch, unpacked / "symbols" / arch)
        dest = prefix / release
        prepared = _descriptors(unpacked, dest, manifest)
        descriptors = prefix / "descriptors"
        descriptors.mkdir(exist_ok=True)
        old = tmp / "replaced"
        if dest.exists():
            os.replace(dest, old)
        os.replace(unpacked, dest)
        try:
            for name, text in prepared:
                _write(descriptors / name, text)
        except BaseException:
            os.replace(dest, tmp / "failed")
            if old.exists():
                os.replace(old, dest)
            raise
    written = [name for name, _ in prepared]
    for f in sorted(descriptors.iterdir()):
        if _OURS.match(f.name) and f.name not in written:
            print(f"removing another release's descriptor {f}")
            f.unlink()
    print(f"installed {release} in {dest}")
    for name in written:
        print(f"descriptor {descriptors / name}")
