"""The command line build.sh calls: python -m axlfw <command> (from lib/)."""

from __future__ import annotations

import argparse
import lzma
import sys
import tarfile
from pathlib import Path


def _pairs(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        key, _, value = v.partition("=")
        out[key] = value
    return out


def main(argv: list[str]) -> int:
    """Run one command; its exit status."""
    p = argparse.ArgumentParser(prog="axlfw")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="the FV check of one image")
    v.add_argument("arch", choices=("x64", "aarch64"))
    v.add_argument("image", type=Path)

    g = sub.add_parser("source-guids", help="compare the checked GUIDs with their INFs")
    g.add_argument("src", type=Path)

    a = sub.add_parser("assemble", help="stage, check and pack a finished build")
    for name in ("ws", "src", "dist", "stage"):
        a.add_argument(f"--{name}", type=Path, required=True)
    for name in ("release", "tag", "commit", "url", "fix", "toolchain", "release-date", "repo-commit", "built-by"):
        a.add_argument(f"--{name}", required=True)
    a.add_argument("--repo-dirty", type=int, required=True)
    for name in ("compiler", "defines", "pcds"):
        a.add_argument(f"--{name}", action="append", default=[])
    a.add_argument("--patch", type=Path, action="append", default=[])
    a.add_argument("--qemu-dir", type=Path)

    c = sub.add_parser("check", help="re-check an unpacked release")
    c.add_argument("--release-dir", type=Path, required=True)
    c.add_argument("--qemu-dir", type=Path)
    c.add_argument("--logs", type=Path)

    i = sub.add_parser("install", help="install a release")
    i.add_argument("--repo", required=True)
    i.add_argument("--from", dest="source", required=True)
    i.add_argument("--prefix", type=Path)
    i.add_argument("--sha256")
    i.add_argument("--symbols", action="store_true")

    args = p.parse_args(argv)
    if args.command == "install":
        from axlfw import install  # noqa: PLC0415 - stdlib only, no uefi_firmware

        try:
            install.install(args.repo, args.source, args.prefix, args.sha256, args.symbols)
        except (install.InstallError, OSError, ValueError, KeyError, TypeError, tarfile.TarError, lzma.LZMAError) as err:
            print(f"build.sh install: {err}", file=sys.stderr)
            return 1
        return 0

    from axlfw import checks, release  # noqa: PLC0415

    if args.command == "verify":
        problems = checks.check_image(args.arch, args.image)
        if problems:
            print("\n".join(f"build.sh: {args.arch}: {x}" for x in problems), file=sys.stderr)
            return 1
        n = len(checks.drivers(args.arch))
        print(f"{args.arch}: {args.image} holds every driver checked ({n}) and none of {len(checks.ABSENT)} absent")
        return 0
    if args.command == "source-guids":
        problems = checks.source_guids(args.src)
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        print(f"{args.src}: every checked GUID is its INF's (or DEC's)")
        return 0
    if args.command == "check":
        logs = args.logs or Path.cwd() / "check-logs"
        return release.check(args.release_dir, args.qemu_dir, logs)
    return release.assemble(
        release.AssembleArgs(
            ws=args.ws,
            src=args.src,
            dist=args.dist,
            stage=args.stage,
            release=args.release,
            tag=args.tag,
            commit=args.commit,
            url=args.url,
            fix=args.fix,
            toolchain=args.toolchain,
            release_date=args.release_date,
            compilers=_pairs(args.compiler),
            defines=_pairs(args.defines),
            pcds=_pairs(args.pcds),
            patches=args.patch,
            repo_commit=args.repo_commit,
            repo_dirty=args.repo_dirty,
            built_by=args.built_by,
            qemu_dir=args.qemu_dir,
        )
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
