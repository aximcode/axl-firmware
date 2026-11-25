# axl-firmware

Prebuilt UEFI firmware (OVMF X64, ArmVirtQemu AARCH64) for QEMU, built from a
pinned edk2 stable tag with this repository's patches, checked property by
property, and published as one GitHub release per tag. Consumed by axlvmpool,
axl-sdk, SoftBMC, AGT and a downstream diagnostics project. Moved here from
axlvmpool (its commit `4ad98ed`, DESIGN §11 items 101 and 102).

## Design doc (MANDATORY)

- `docs/DESIGN.md` is the spec and the decision record. Read the relevant
  sections before changing behaviour; update it in the same commit when a
  decision changes.
- Its §4 lists decisions already taken: do not re-litigate them. §5 lists the
  owner's open decisions, each with a recommendation: ask, don't decide, and
  record the answer there. §6 lists assumptions: record how each was verified.
- `docs/RELEASE-FORMAT.md` is the consumers' contract. **Consult the consumers
  (axlvmpool, axl-sdk, SoftBMC, AGT, the downstream project) before changing
  the release format**, the descriptors or the install layout.
- `docs/PROPERTIES.md` lists every checked property with its sabotage; a new
  property goes there with its own.

## Development

- Test-first: the failing test, RED, then the code, GREEN. Exact assertions
  (whole values, exact strings).
- A check's silence is worth nothing until it has been shown to fail: every new
  check gets a control or a sabotage run, recorded in `docs/PROPERTIES.md`.
  Restore sabotaged files with `cp` plus `touch`, and check them by sha256.
- An independent review (a read-only agent given the code, not your
  conclusions) before each release.
- Gates before commit: `shellcheck build.sh tests/firmware-tests.sh`,
  `pyright` (strict, 0 errors), `tests/firmware-tests.sh`.
- Python: `from __future__ import annotations`, full annotations, f-strings,
  pathlib, `if __name__ == "__main__":`. `lib/axlfw/install.py` stays
  standard-library only and Python 3.9-compatible.
- **Never write into a shared edk2 tree** (`--edk2 DIR`): the build is out of
  tree, and must stay so.
- Heavy work runs niced (`nice -n 19`, `ionice -c3`), at most 4 build jobs. On a
  shared host, check for other heavy jobs (CI runners) first; prefer letting
  GitHub-hosted CI build.
- QEMU boot checks never kill a QEMU they did not start.

## Commits and files

No references to Claude and no Co-Authored-By trailers. No host names, LAN
IPs, personal machine names or home paths in commits, code or docs: use
`$HOME`, XDG variables or parameters. Measured claims name the platform
("AlmaLinux 10 x86_64, QEMU 10.0.0"). No downstream company names or their
GUIDs. Run `git status` first; stage explicit paths.
