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

import contextlib
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
# A release's image sets, by directory: DEBUG at the top, RELEASE under
# release/. Every one must be there.
IMAGE_DIRS = ("x64", "aarch64", "release/x64", "release/aarch64")


def descriptor_name(d: str, release: str) -> str:
    """The file name of the descriptor of the image set in directory d.

    DEBUG's sort first (10-), RELEASE's after (20-).
    """
    if d.startswith("release/"):
        return f"20-axl-firmware-{release}-release-{d[len('release/'):]}.json"
    return f"10-axl-firmware-{release}-{d}.json"


def features(d: str) -> list[str]:
    """The features of the descriptor of the image set in d: only what the build has.

    X64 as QEMU labels its own build with these flags (S3, SEV, SEV-ES);
    DEBUG adds its debug output: X64's on the 0x402 debug port
    (verbose-dynamic), AARCH64's on the serial port by static configuration
    (verbose-static; errors only, but static serial output all the same).
    RELEASE has none.
    """
    arch = d.rsplit("/", 1)[-1]
    base = ["acpi-s3", "amd-sev", "amd-sev-es"] if arch == "x64" else []
    if not d.startswith("release/"):
        base.append("verbose-dynamic" if arch == "x64" else "verbose-static")
    return base


def descriptor(d: str, release: str, tag: str, commit: str, root: str) -> dict[str, object]:
    """The QEMU firmware descriptor (docs/interop/firmware.json) of the set in d.

    root prefixes the images' paths: "" in the tarball, the install
    directory (with a trailing slash) once installed.
    """
    arch = d.rsplit("/", 1)[-1]
    flavour = "RELEASE" if d.startswith("release/") else "DEBUG"
    what = "X64 (q35)" if arch == "x64" else "AARCH64 (virt)"
    files = image_files(d)
    return {
        "description": f"axl-firmware {release} for {what}, {flavour}, edk2 {tag}",
        "interface-types": ["uefi"],
        "mapping": {
            "device": "flash",
            "mode": "split",
            "executable": {"filename": f"{root}{files['code']}", "format": "raw"},
            "nvram-template": {"filename": f"{root}{files['vars']}", "format": "raw"},
        },
        "targets": [
            {
                "architecture": "x86_64" if arch == "x64" else "aarch64",
                "machines": ["pc-q35-*"] if arch == "x64" else ["virt-*"],
            }
        ],
        "features": features(d),
        "tags": [tag, commit, release, flavour],
    }


def json_object(value: object) -> dict[str, object] | None:
    """value, if it is a JSON object; else None."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def image_files(d: str) -> dict[str, str]:
    """The files of the image set in directory d, by manifest key."""
    arch = d.rsplit("/", 1)[-1]
    return {"code": f"{d}/code.fd", "vars": f"{d}/vars.fd", "shell": f"{d}/Shell_{arch.upper()}.efi"}


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


def _verify(path: Path, sha: str | None, shown: Path) -> None:
    """path's sha256 is sha, or else the one SHA256SUMS beside it lists.

    shown is where path came from, for the messages.
    """
    want = sha
    if want is None:
        sums = path.parent / "SHA256SUMS"
        if not sums.is_file():
            msg = f"{shown}: no SHA256SUMS beside it; give its sha256 with --sha256"
            raise InstallError(msg)
        want = _sums(sums).get(path.name)
        if want is None:
            msg = f"{shown.parent / 'SHA256SUMS'} does not list {path.name}"
            raise InstallError(msg)
    got = _sha256(path)
    if got != want.lower():
        msg = f"{shown}: sha256 {got}, expected {want}"
        raise InstallError(msg)


def _extract(tarball: Path, top: str, into: Path, shown: Path) -> Path:
    """Unpack tarball, whose every member must sit under top/, into into.

    shown is where tarball came from, for the messages.
    """
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
                msg = f"{shown}: unexpected member {m.name!r}"
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
    manifest = json_object(json.loads((root / "manifest.json").read_text()))
    if manifest is None:
        msg = f"{root}: its manifest is not a JSON object"
        raise InstallError(msg)
    if manifest.get("format") != FORMAT:
        msg = f"{root}: manifest format {manifest.get('format')!r}; this install reads format {FORMAT}"
        raise InstallError(msg)
    if manifest.get("release") != release:
        msg = f"{root}: its manifest names release {manifest.get('release')!r}, not {release!r}"
        raise InstallError(msg)
    images = json_object(manifest.get("images"))
    if images is None or sorted(images) != sorted(IMAGE_DIRS):
        got = manifest.get("images") if images is None else sorted(images)
        msg = f"{root}: its manifest's images are {got}, not {sorted(IMAGE_DIRS)}"
        raise InstallError(msg)
    for d in IMAGE_DIRS:
        entry = json_object(images[d]) or {}
        for key, want in image_files(d).items():
            f = json_object(entry.get(key)) or {}
            got = f.get("path")
            if got != want:
                msg = f"{root}: its manifest names {got!r} for {d}'s {key}, not {want!r}"
                raise InstallError(msg)
            p = _inside(root, want)
            if not p.is_file() or p.stat().st_size != f.get("size") or _sha256(p) != f.get("sha256"):
                msg = f"{p}: not {d}'s {key} as its manifest records it"
                raise InstallError(msg)
    return manifest


def _descriptors(unpacked: Path, dest: Path, manifest: dict[str, object], release: str) -> list[tuple[str, str]]:
    """Each descriptor of the release, its paths made absolute under dest.

    The manifest must list one per image set, by its name, and each must be
    exactly that set's descriptor. Checked against the unpacked release
    before anything is replaced.
    """
    edk2 = json_object(manifest.get("edk2")) or {}
    tag, commit = edk2.get("tag"), edk2.get("commit")
    if not isinstance(tag, str) or not isinstance(commit, str):
        msg = f"{unpacked}: its manifest has no edk2.tag and edk2.commit"
        raise InstallError(msg)
    want = {f"descriptors/{descriptor_name(d, release)}": d for d in IMAGE_DIRS}
    listed = manifest.get("descriptors")
    if not isinstance(listed, list) or sorted(str(x) for x in cast("list[object]", listed)) != sorted(want):
        msg = f"{unpacked}: its manifest's descriptors are {listed!r}, not {sorted(want)}"
        raise InstallError(msg)
    out: list[tuple[str, str]] = []
    for rel, d in want.items():
        name = Path(rel).name
        desc = json_object(json.loads((unpacked / rel).read_text())) or {}
        mapping = json_object(desc.get("mapping")) or {}
        files = image_files(d)
        for key, own in (("executable", files["code"]), ("nvram-template", files["vars"])):
            entry = json_object(mapping.get(key)) or {}
            f = entry.get("filename")
            if f != own:
                msg = f"{unpacked / rel}: its {key} is {f!r}, not {own!r}"
                raise InstallError(msg)
            if not _inside(unpacked, own).is_file():
                msg = f"{unpacked / rel}: names {own}, which is not in the release"
                raise InstallError(msg)
            entry["filename"] = str(dest / own)
        if desc != descriptor(d, release, tag, commit, f"{dest}/"):
            msg = f"{unpacked / rel}: not the descriptor of {d}"
            raise InstallError(msg)
        out.append((name, json.dumps(desc, indent=2) + "\n"))
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
        # Everything is read from a private copy: what is verified is what
        # is unpacked, whatever happens to the files given.
        fetched = tmp / "fetched"
        fetched.mkdir()
        local = Path(source)
        if local.is_file():
            m = re.match(r"^axl-firmware-(.+)\.tar\.xz$", local.name)
            if not m or not _TAG.match(m.group(1)):
                msg = f"{local}: not named axl-firmware-<release>.tar.xz, with a release edk2-stableYYYYMM-rN"
                raise InstallError(msg)
            release = m.group(1)
            given = local.parent
            names = [local.name, "SHA256SUMS"]
            if symbols:
                if not (given / "SHA256SUMS").is_file():
                    msg = (
                        f"{local}: --symbols needs SHA256SUMS beside it, for the symbols tarballs' digests"
                        " (--sha256 gives only this tarball's)"
                    )
                    raise InstallError(msg)
                names += [f"axl-firmware-{release}-symbols-{a}.tar.xz" for a in ("x64", "aarch64")]
            for n in names:
                if (given / n).is_file():
                    shutil.copyfile(given / n, fetched / n)
                elif n != "SHA256SUMS":
                    msg = f"{given / n}: no such file"
                    raise InstallError(msg)
        elif _TAG.match(source):
            release = source
            given = fetched
            names = [f"axl-firmware-{release}.tar.xz", "SHA256SUMS"]
            if symbols:
                names += [f"axl-firmware-{release}-symbols-{a}.tar.xz" for a in ("x64", "aarch64")]
            print(f"fetching {repo} {release}")
            download(repo, release, names, fetched)
        else:
            msg = f"--from {source}: no such file, and not a release tag (edk2-stableYYYYMM-rN)"
            raise InstallError(msg)
        path = fetched / names[0]
        _verify(path, sha, given / path.name)
        print(f"{path.name}: sha256 verified")
        top = f"axl-firmware-{release}"
        unpacked = _extract(path, top, tmp, given / path.name)
        manifest = _check_set(unpacked, release)
        if symbols:
            for arch in ("x64", "aarch64"):
                sym = fetched / f"axl-firmware-{release}-symbols-{arch}.tar.xz"
                _verify(sym, None, given / sym.name)
                part = _extract(sym, f"{top}-symbols-{arch}", tmp, given / sym.name)
                (unpacked / "symbols").mkdir(exist_ok=True)
                os.replace(part / arch, unpacked / "symbols" / arch)
        dest = prefix / release
        prepared = _descriptors(unpacked, dest, manifest, release)
        descriptors = prefix / "descriptors"
        descriptors.mkdir(exist_ok=True)
        # A descriptor of the same name is the same release's: kept to put
        # back if this install fails.
        previous = {name: (descriptors / name).read_text() for name, _ in prepared if (descriptors / name).is_file()}
        old = tmp / "replaced"
        moved_in = False
        written: list[str] = []
        try:
            if dest.exists():
                os.replace(dest, old)
            os.replace(unpacked, dest)
            moved_in = True
            for name, text in prepared:
                written.append(name)
                _write(descriptors / name, text)
        except BaseException:
            # No descriptor may name what is not there: each one written
            # goes, or back to what it was.
            for name in written:
                with contextlib.suppress(OSError):
                    if name in previous:
                        _write(descriptors / name, previous[name])
                    else:
                        (descriptors / name).unlink(missing_ok=True)
            if moved_in:
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
