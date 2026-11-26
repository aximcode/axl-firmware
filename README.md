# axl-firmware

Prebuilt UEFI firmware for QEMU: OVMF for X64 (`q35`) and ArmVirtQemu (AAVMF)
for AARCH64 (`virt`), built from a pinned edk2 stable tag with QEMU's own build
flags and fourteen small patches, checked property by property, and published as one
GitHub release per tag.

No downloadable image has what the aximcode projects need: VirtioFsDxe and
VirtioGpuDxe (distribution images leave them out), edk2's VirtioRng fix
`8ba02634` (QEMU's bundled images predate it), pointer drivers, and video on
AARCH64's `bochs-display`. Building edk2 should be no developer's job, so this
repository builds it once in CI and every project installs the result.
[docs/DESIGN.md](docs/DESIGN.md) has the reasons and the decisions.

**Consumers:** axlvmpool (its warm guest pool), axl-sdk (its QEMU runs, tests
and debug tooling), SoftBMC, AGT, and a downstream diagnostics project. How each
finds the firmware: [docs/CONSUMERS.md](docs/CONSUMERS.md).

## Quick start

```sh
git clone git@github.com:aximcode/axl-firmware.git && cd axl-firmware
./build.sh install --from edk2-stable202611-r3
```

The repository is public: the download needs no login. It uses `gh` when it is
installed and logged in, the REST API with `GITHUB_TOKEN` when that is set, and
otherwise the plain release URL (`https://github.com/aximcode/axl-firmware/releases/download/<tag>/<asset>`). The install verifies the tarball's sha256 against the release's
`SHA256SUMS` (or `--sha256 <hex>`, to pin it yourself), then writes:

```
$XDG_DATA_HOME/axl-firmware/                     ($HOME/.local/share/axl-firmware by default)
  edk2-stable202611-r3/                          the release, unpacked
    x64/{code.fd,vars.fd,Shell_X64.efi}          DEBUG (the default)
    aarch64/{code.fd,vars.fd,Shell_AARCH64.efi}  DEBUG
    release/x64/...  release/aarch64/...         RELEASE
    guarded/x64/...  guarded/aarch64/...         GUARDED: DEBUG with the heap guard, no descriptor
    manifest.json  descriptors/  LICENSES/  patches/
  descriptors/                                   QEMU firmware descriptors, absolute paths
    10-axl-firmware-edk2-stable202611-r3-{x64,aarch64}.json           DEBUG
    20-axl-firmware-edk2-stable202611-r3-release-{x64,aarch64}.json   RELEASE
```

`--prefix DIR` installs elsewhere; `--symbols` also unpacks the DEBUG symbols
(`symbols/<arch>/*.debug`). Installing a release removes every other release's
descriptors (their files stay), so one release is advertised at a time.

To boot it by hand:

```sh
fw=$HOME/.local/share/axl-firmware/edk2-stable202611-r3/x64
cp "$fw/vars.fd" /tmp/vars.fd
qemu-system-x86_64 -machine q35 -m 1G -nographic \
  -drive if=pflash,format=raw,unit=0,readonly=on,file="$fw/code.fd" \
  -drive if=pflash,format=raw,unit=1,file=/tmp/vars.fd
```

## A release at a glance

| Asset | What |
|---|---|
| `axl-firmware-<release>.tar.xz` | both arches, DEBUG, RELEASE and GUARDED (DEBUG with the heap guard, for finding memory corruption; selected by path, never by a descriptor): code, vars template and UEFI Shell each; `manifest.json`; QEMU descriptors; the patches; licences |
| `axl-firmware-<release>-symbols-{x64,aarch64}.tar.xz` | the DEBUG build's `*.debug` files and FV maps, for gdb and crash decoders |
| `SHA256SUMS` | the three tarballs' sha256 |

A release tag is `<edk2 tag>-r<N>` (`edk2-stable202611-r3`). The exact format,
every manifest field included, is [docs/RELEASE-FORMAT.md](docs/RELEASE-FORMAT.md).
What each release is checked to hold, and how each check was shown to fail on a
broken image, is [docs/PROPERTIES.md](docs/PROPERTIES.md).

## Building from source

```sh
./build.sh build              # fetch the pinned edk2, build, check, pack into dist/
./build.sh build --install    # and install the result, as above
./build.sh build --edk2 DIR   # against an existing tree at the pinned tag, out of tree
```

The build never writes into the edk2 source. Prerequisites, the out-of-tree
layout, CI, and the procedure for moving to a new edk2 tag:
[docs/BUILDING.md](docs/BUILDING.md).

## Other commands

| Command | What |
|---|---|
| `build.sh check <dir>` | re-run every image and boot check on an unpacked release |
| `build.sh verify <x64\|aarch64> <image>` | the driver check of one code image |
| `build.sh check-source <dir>` | is this edk2 tree the pinned source, as committed? |
| `build.sh fetch` | only fetch the pinned edk2 source into the cache, or check the copy there |
| `tests/firmware-tests.sh` | the tests that need no build |

## Licences

The scripts in this repository are Apache-2.0 ([LICENSE](LICENSE)). The firmware
is edk2's, BSD-2-Clause-Patent, with bundled third-party parts under their own
licences; every release carries them in `LICENSES/` (OpenSSL, libfdt, libtl
and others), and its manifest lists, per image, the licence texts that image
needs (`images.<dir>.licenses`, derived from the build: RELEASE-FORMAT.md).

## Changes

[CHANGELOG.md](CHANGELOG.md).
