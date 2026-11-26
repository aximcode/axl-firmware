# Release format

The consumers' contract. A change here is agreed with the consumers first
([CONSUMERS.md](CONSUMERS.md)) and recorded in [DESIGN.md](DESIGN.md).

**Read by consumers today, so stable** (a field may be added; none of these is
removed, renamed or reshaped without the consumers agreeing first):

| What | Read by |
|---|---|
| asset names `axl-firmware-<release>.tar.xz`, `…-symbols-<arch>.tar.xz`, `SHA256SUMS` | every installer and vendoring script |
| the layout `x64/`, `aarch64/`, `release/x64/`, `release/aarch64/`, each `code.fd`, `vars.fd`, `Shell_<ARCH>.efi` | every consumer |
| `manifest.json` `format` (2), `release`, `edk2.{tag,commit}`, `images.<dir>.{flavour,code,vars,shell}` with `size` and `sha256` | vendoring and provenance (axlvmpool's `axlvmpool firmware`, a downstream vendoring script) |
| `images.<dir>.licenses` (`[{path, chosen}]`) | a downstream vendoring script |
| `hardware_images` (`{<arch>: <image dir>}`, from r3) | SoftBMC, from r3 (the images it deploys onto physical machines; stable by that commitment, before its first read) |
| `images.<dir>.measured.text_modes.<display>.{modes,current}` (`modes: [[cols, rows], …]`, `current: [cols, rows]`) | axl-sdk's interactive launcher (console fit, 2026-11-25) |
| `images.x64.flavour` (`DEBUG`), `images.x64.{code,vars}.path`, `images.x64.debug.{asserts_compiled,clear_memory}` | axl-sdk's `find_debug_ovmf` |
| `images.<dir>.code.path` (matched to the firmware in use), `images.<dir>.shell.{path,sha256}` | axl-sdk's `find_shell_efi` |
| every `path` relative to the release directory, never escaping it; `manifest.json` at the release directory's top, found from an image within three directories | axl-sdk |
| the descriptors' file names and numbering (DEBUG `10-`, RELEASE `20-`) | the shared descriptor search (axlvmpool, axl-sdk) |
| `guarded/x64/`, `guarded/aarch64/` (from r3), each `code.fd`, `vars.fd`, `Shell_<ARCH>.efi`, with **no descriptor**; `images.guarded/<arch>` with `flavour` `GUARDED` | SoftBMC, by path (stable by that commitment, before its first read) |

## The release

A release is a GitHub release of `aximcode/axl-firmware`, tagged
`<edk2 tag>-r<N>` (`edk2-stable202611-r1`): `<N>` counts this repository's
releases of one edk2 tag. Its assets:

| Asset | Contents |
|---|---|
| `axl-firmware-<release>.tar.xz` | the release (below) |
| `axl-firmware-<release>-symbols-x64.tar.xz` | X64 DEBUG symbols |
| `axl-firmware-<release>-symbols-aarch64.tar.xz` | AARCH64 DEBUG symbols |
| `SHA256SUMS` | `<sha256>  <asset>` per tarball, in name order (`sha256sum -c` format) |

## The release tarball

Every member sits under one directory, `axl-firmware-<release>/`. Members are
regular files and directories only, owned by 0:0, files `0644`, directories
`0755`, each dated the edk2 commit's date, in the order of their path
components (Python's `sorted` over `pathlib` paths). Only the member list and
metadata are deterministic: `manifest.json` records the build time and run, so
two builds' tarballs differ even when their images are identical.

```
axl-firmware-<release>/
  x64/code.fd                 OVMF_CODE.fd, X64 DEBUG
  x64/vars.fd                 OVMF_VARS.fd, its variable-store template
  x64/Shell_X64.efi           the UEFI Shell of that build
  aarch64/code.fd             QEMU_EFI.fd, AARCH64 DEBUG, padded with zeros to 64 MiB
  aarch64/vars.fd             QEMU_VARS.fd, padded to 64 MiB
  aarch64/Shell_AARCH64.efi
  release/x64/...             the same three files, RELEASE
  release/aarch64/...         the same three files, RELEASE
  guarded/x64/...             the same three files, GUARDED (from r3; no descriptor)
  guarded/aarch64/...         the same three files, GUARDED (from r3; no descriptor)
  descriptors/10-axl-firmware-<release>-x64.json              X64 DEBUG
  descriptors/10-axl-firmware-<release>-aarch64.json          AARCH64 DEBUG
  descriptors/20-axl-firmware-<release>-release-x64.json      X64 RELEASE
  descriptors/20-axl-firmware-<release>-release-aarch64.json  AARCH64 RELEASE
  manifest.json
  patches/*.patch             the patches applied to edk2
  LICENSES/edk2-License.txt   edk2's (BSD-2-Clause-Patent)
  LICENSES/<submodule>/...    each third-party part the build compiled: its
                              licence files, the submodule's path with / as _
                              (which of them each image needs: images.<dir>.licenses)
```

The DEBUG directories are the bare arch names because DEBUG is the default
every consumer boots; RELEASE sits apart under `release/`, and GUARDED
under `guarded/`. Each directory holds
its code, its vars template and its Shell together, because consumers take the
firmware's directory as the Shell's (`${firmware_dir}/Shell_<ARCH>.efi`).

## The guarded image (GUARDED, from r3)

`guarded/<arch>` is the DEBUG build with edk2's UEFI heap guard: a write past
a pool buffer or a page allocation of a driver or application faults at the
instruction that makes it, where DEBUG and RELEASE corrupt memory silently
and may fail much later, or only on real hardware. Its build differs from
DEBUG's only by three FixedAtBuild PCDs (`images.guarded/<arch>.pcds` lists
them; `debug.heap_guard` states what its DxeMain's AutoGen holds):

| PCD | Value | Meaning |
|---|---|---|
| `PcdHeapGuardPropertyMask` | `0x03` | the UEFI page guard (BIT0) and pool guard (BIT1); BIT7 clear, so a pool buffer is placed against its *tail* guard page, and a write one byte past a buffer whose size is a multiple of 8 faults. Not the freed-memory guard (BIT4, use-after-free), which edk2 cannot enable with the pool and page guards; not non-stop mode (BIT6) |
| `PcdHeapGuardPoolType` | `0x1E` | pool allocations of EfiLoaderCode, EfiLoaderData, EfiBootServicesCode and EfiBootServicesData |
| `PcdHeapGuardPageType` | `0x1E` | page allocations of the same types |

- **Never picked by a descriptor search.** It has no descriptor, so no
  search (QEMU's, libvirt's, axlvmpool's or axl-sdk's) can pick it, even when
  every described image fails a consumer's checks. Select it by path:
  `guarded/<arch>/code.fd` and `guarded/<arch>/vars.fd` under the release
  ([CONSUMERS.md](CONSUMERS.md)).
- **What it does not catch.** An overrun into the slack of a buffer whose
  size is not a multiple of 8 (the pool's 8-byte alignment) lands before the
  guard page; a write *before* a buffer (the guard is at the tail);
  use-after-free; the runtime memory types (EfiRuntimeServicesCode and
  Data, which AARCH64's 64 KiB runtime granularity cannot guard); SMM (not
  built). A guarded pool has no pool tail (edk2's `CoreAllocatePoolI` drops
  it when the buffer sits against its tail guard), so an overrun into the
  alignment slack of a guarded type is caught by nothing, where on DEBUG it
  may hit the tail's signature and ASSERT when the buffer is freed.
- **Its cost**: [PROPERTIES.md](PROPERTIES.md) and DESIGN.md §4.34 give the
  boot time and memory measured against DEBUG.
- **Its symbols** are DEBUG's, but for the three modules the PCDs change
  (DxeCore, DxeIpl and, on X64, CpuDxe): no symbols tarball is published for
  it.

## Descriptors

QEMU firmware descriptors (QEMU's `docs/interop/firmware.json`), one per image
but GUARDED, which has none.
In the tarball their file names are **relative to the release directory**; the
install rewrites them to absolute paths, which QEMU requires. Installed X64 DEBUG
(prefix `$HOME/.local/share/axl-firmware`, written out):

```json
{
  "description": "axl-firmware edk2-stable202611-r1 for X64 (q35), DEBUG, edk2 edk2-stable202611",
  "interface-types": ["uefi"],
  "mapping": {
    "device": "flash",
    "mode": "split",
    "executable": {"filename": "$HOME/.local/share/axl-firmware/edk2-stable202611-r1/x64/code.fd", "format": "raw"},
    "nvram-template": {"filename": "$HOME/.local/share/axl-firmware/edk2-stable202611-r1/x64/vars.fd", "format": "raw"}
  },
  "targets": [{"architecture": "x86_64", "machines": ["pc-q35-*"]}],
  "features": ["acpi-s3", "amd-sev", "amd-sev-es", "verbose-dynamic"],
  "tags": ["edk2-stable202611", "556dcfb466e9f05affeaff881d738807f8156d3c", "edk2-stable202611-r1", "DEBUG"]
}
```

| Image | File | `architecture`, `machines` | `features` |
|---|---|---|---|
| X64 DEBUG | `10-axl-firmware-<release>-x64.json` | `x86_64`, `pc-q35-*` | `acpi-s3`, `amd-sev`, `amd-sev-es`, `verbose-dynamic` |
| AARCH64 DEBUG | `10-axl-firmware-<release>-aarch64.json` | `aarch64`, `virt-*` | `verbose-static` |
| X64 RELEASE | `20-axl-firmware-<release>-release-x64.json` | `x86_64`, `pc-q35-*` | `acpi-s3`, `amd-sev`, `amd-sev-es` |
| AARCH64 RELEASE | `20-axl-firmware-<release>-release-aarch64.json` | `aarch64`, `virt-*` | (none) |

- `description`: `axl-firmware <release> for <X64 (q35)|AARCH64 (virt)>, <flavour>, edk2 <edk2 tag>`.
- `tags`: the edk2 tag, its commit, the release, the flavour.
- **Numbering:** DEBUG `10-`, RELEASE `20-`. Under the shared rule (lowest name
  first, `verbose-static` first on AARCH64) DEBUG is picked; RELEASE is chosen
  by name. The `10-axl-firmware-` prefix sorts before a distribution's
  `10-edk2-...`.
- Features are only what the build has: S3 and SEV/SEV-ES as QEMU labels its
  own build with these flags; `verbose-dynamic` for X64 DEBUG (its DEBUG output
  on I/O port 0x402); `verbose-static` for AARCH64 DEBUG (errors only, on the
  second UART by static configuration). No `secure-boot`, no `enrolled-keys`.

## manifest.json

`format` 2. (Format 1 was axlvmpool's own manifest, at its old install path.)
Paths are relative to the release directory. Every value below is present.

| Field | Type | Meaning |
|---|---|---|
| `format` | int | 2 |
| `release` | string | the release tag, `edk2-stable202611-r1` |
| `edk2.tag` | string | the edk2 tag built |
| `edk2.commit` | string | its commit (full hash) |
| `edk2.url` | string | the repository it was fetched from |
| `edk2.fixes` | [string] | fixes the source must hold as ancestors of `commit`, full hashes: the VirtioRng fix `8ba02634…` and the LPA2 fix `b8df7d9c…` |
| `edk2.patches` | [{`name`, `sha256`}] | the patches applied, in order (`patches/<name>`) |
| `flags_from` | string | where the build flags come from |
| `built` | string | UTC build time, `YYYY-MM-DDTHH:MM:SSZ` |
| `built_by.how` | string | `local`, or `github-actions <run URL>` |
| `built_by.axl_firmware_commit` | string | this repository's commit; `none` when the build ran outside a git checkout git accepts |
| `built_by.uncommitted_changes` | int | files changed in that checkout (0 for a release); -1 when git could not tell, as above |
| `toolchain` | string | edk2's toolchain tag, `GCC` |
| `compilers.x64`, `compilers.aarch64` | string | the compiler and version used |
| `default_flavour` | string | `DEBUG` |
| `hardware_images` | {string: string} | from r3: per arch (`x64`, `aarch64`), the directory of the image to ship to real hardware (the Shell and firmware deployed onto physical machines, where a DEBUG build's ASSERT can halt the box): `{"x64": "release/x64", "aarch64": "release/aarch64"}`. Each names one of `images`, of its own arch, whose `flavour` is `RELEASE` (checked: a release whose entry names a missing image, another arch's or one not RELEASE is not packed). `default_flavour` is what QEMU guests boot, and stays `DEBUG` |
| `images` | object | one entry per image, keyed by its directory: `x64`, `aarch64`, `release/x64`, `release/aarch64`, and from r3 `guarded/x64`, `guarded/aarch64`. An installer requires the first four and checks every entry, each `<name>/<arch>` (or the bare arch): a later release may add another image set |
| `descriptors` | [string] | the descriptors, `descriptors/<name>` |
| `symbols.x64`, `symbols.aarch64` | string | the symbols tarballs' asset names |
| `licenses` | [string] | the licence files, `LICENSES/...`: edk2's, and every file of each third-party part (edk2 submodule) whose sources the build compiled or whose headers it used; a superset of every image's `licenses` |

Each `images.<dir>`:

| Field | Type | Meaning |
|---|---|---|
| `arch` | string | `x64` or `aarch64` |
| `flavour` | string | `DEBUG`, `RELEASE` or (from r3) `GUARDED` |
| `machine` | string | QEMU's machine: `q35`, `virt` |
| `platform` | string | the DSC: `OvmfPkg/OvmfPkgX64.dsc`, `ArmVirtPkg/ArmVirtQemu.dsc` |
| `defines` | [string] | the build's `-D` defines |
| `pcds` | [string] | the build's `--pcd` overrides, as given to `build` (GUARDED's add the heap-guard PCDs) |
| `code`, `vars`, `shell` | {`path`, `size`, `sha256`} | the three files |
| `descriptor` | string or null | its descriptor, `descriptors/<name>`; null for GUARDED, which has none |
| `secure_boot` | bool | `false`: no Secure Boot is built in |
| `debug.target` | string | the build's target, `DEBUG` or `RELEASE` (GUARDED's is `DEBUG`) |
| `debug.asserts_compiled` | bool | ASSERTs and DEBUG are compiled in (no `MDEPKG_NDEBUG`) |
| `debug.property_mask` | string or null | `PcdDebugPropertyMask`, hex (`0x2f`); null when the build references none (AARCH64 RELEASE) |
| `debug.print_error_level` | string or null | `PcdDebugPrintErrorLevel`, hex (`0x8000004f`, `0x80000000`); null as above |
| `debug.boot_manager_menu` | string | `UiApp` when `PcdBootManagerMenuFile` is UiApp's GUID |
| `debug.assert` | string | X64 DEBUG and GUARDED only: `deadloop` or `breakpoint` (the mask's 0x20 or 0x10) |
| `debug.clear_memory` | bool | X64 DEBUG and GUARDED only: the mask's 0x08 (freed memory cleared) |
| `debug.output` | string | DEBUG and GUARDED only: where the DEBUG output goes |
| `debug.descriptor_feature` | string | DEBUG only: `verbose-dynamic` or `verbose-static` |
| `debug.heap_guard` | {`property_mask`, `pool_type`, `page_type`} | GUARDED only, from r3: `PcdHeapGuardPropertyMask`, `PcdHeapGuardPoolType`, `PcdHeapGuardPageType` as its DxeMain's AutoGen holds them, hex (`0x03`, `0x1e`, `0x1e`) |
| `verified.by` | string | the walker that read the image |
| `verified.drivers` | [{`name`, `guid`}] | the FFS files the code image was checked to hold |
| `verified.absent` | [{`name`, `guid`}] | the FFS files it was checked not to hold |
| `measured` | object | what the boot checks measured (below) |
| `licenses` | [{`path`, `chosen`}] | the licence texts this image needs (below), in order: edk2's first, then each part's files by the part's path |

Each `images.<dir>.licenses` entry, from r2 on:

| Field | Type | Meaning |
|---|---|---|
| `path` | string | a licence file, relative to the release directory, `LICENSES/...`; always one of the top-level `licenses` |
| `chosen` | bool | `false` only for the licence a dual-licensed part is *not* taken under; `true` for every other file |

The list holds `LICENSES/edk2-License.txt` always, and every file of each
third-party part compiled into a module of the image's code firmware
device, nested (compressed) volumes included, through the module's own
sources or a library linked into it. A part whose sources no module of the
image compiles is not listed. The dual-licensed parts are taken under their
permissive licence: libfdt (`MdePkg/Library/BaseFdtLib/libfdt`) under
BSD-2-Clause (`BSD-2-Clause` true, `GPL` false), libtl
(`MdePkg/Library/ArmTransferListLib/libtl`) under MIT (`LICENSE-MIT.md`
true, `LICENSE-GPL-2.0-or-later.md` false; its `LICENSE.md`, which states
the choice, true). Mbed TLS ships both its licences in one `LICENSE`, so it
is one entry, true. A consumer that vendors some images copies the union of
their lists, each `path` as it is, and may leave out the `chosen: false`
texts. The shape is stable: a field may be added, none removed or changed.
How the list is derived and checked: [PROPERTIES.md](PROPERTIES.md),
DESIGN.md §4.22.

r2's lists (DEBUG and RELEASE alike):

| Image | `licenses` |
|---|---|
| `x64`, `release/x64` | `edk2-License.txt`; `CryptoPkg_Library_OpensslLib_openssl/LICENSE.txt` |
| `aarch64`, `release/aarch64` | `edk2-License.txt`; `CryptoPkg_Library_OpensslLib_openssl/LICENSE.txt`; `MdePkg_Library_ArmTransferListLib_libtl/` `LICENSE-GPL-2.0-or-later.md` (false), `LICENSE-MIT.md`, `LICENSE.md`; `MdePkg_Library_BaseFdtLib_libfdt/` `BSD-2-Clause`, `GPL` (false) |

Each `images.<dir>.measured`:

| Field | Type | Meaning |
|---|---|---|
| `text_modes.<display>.modes` | [[cols, rows]] | the Shell's `mode` list, in mode order, per display: `serial` (no display device), `std-vga` (`-device VGA,edid=off`, so the firmware's own default mode, 1280x800, applies), `virtio-gpu` (`-device virtio-gpu-pci`), `bochs-display` (`-device bochs-display,edid=off`, likewise) |
| `text_modes.<display>.current` | [cols, rows] | the mode the console was in |
| `smbios_bios_version` | string | SMBIOS type 0's BIOS version (`smbiosview -t 0`) |
| `nv_after_warm_reset`, `nv_after_cold_start` | bool | a variable written with `setvar -nv` read back after `reset`, and in a new QEMU on the same vars file |
| `front_page_after_exit` | bool | the Shell's `exit` reached the front page |
| `debug_output` | {`where`, `bytes`, `info_lines`} | what reached the DEBUG channel in the serial boot |
| `gop_std_vga` | {`size`: [w, h], `drawn`: bool} | std-VGA's screen at the Shell (`edid=off`), from a screendump: its size (1280x800 on both arches) and whether any pixel is lit |
| `bochs_display` | {`devices`, `size`: [w, h], `drawn`: bool} | QemuVideoDxe's device count on `bochs-display`, and its screen at the Shell (`edid=off`, so the firmware's default, 1280x800 on both arches), from a screendump: its size and whether any pixel is lit |
| `cpu_models_reach_shell` | {model: bool} | AARCH64 only: a boot under each `-cpu` (`max`, `cortex-a57`, `neoverse-v1`) reached the Shell |
| `pool_overrun` | string | DEBUG and GUARDED only, from r3: what a write one byte past a 64-byte pool buffer did (the guard check's application, run from the Shell): `fault` (GUARDED: the CPU exception, at the write) or `silent` (DEBUG: nothing noticed) |

A consumer checks an installed image as the install does: the manifest parses
with `format` 2, lists the four image sets `x64`, `aarch64`, `release/x64` and
`release/aarch64` (and may list more, each `<name>/<arch>`, the name none of
`descriptors`, `symbols`, `patches`, `LICENSES`), and each set's
`code`, `vars` and `shell` are at `<dir>/code.fd`, `<dir>/vars.fd` and
`<dir>/Shell_<ARCH>.efi` with the recorded `size` and `sha256`.

## The symbols tarballs

`axl-firmware-<release>-symbols-<arch>.tar.xz`, under
`axl-firmware-<release>-symbols-<arch>/<arch>/`: the DEBUG build's `<Module>.debug`
files (ELF with DWARF, one per module, as edk2 names them) and `FV/*.Fv.map`
(each firmware volume's module load offsets). `build.sh install --symbols`
puts them in `<prefix>/<release>/symbols/<arch>/`.

## The install layout

```
$XDG_DATA_HOME/axl-firmware/            (default $HOME/.local/share/axl-firmware; --prefix)
  <release>/                            the tarball's directory, unpacked whole
    symbols/<arch>/                     with --symbols
  descriptors/<name>.json               the release's descriptors, absolute paths
```

One install runs at a time per prefix (a lock on `<prefix>/.lock`). The
tarballs and `SHA256SUMS` are copied (or downloaded) into a private directory
beside their place (`<prefix>/.install.*`), and only those copies are read.
The release is unpacked there and checked: the tarball's sha256; its members,
each as its header is read and before any is written (regular files and
directories under its one top directory, no `..`, not absolute, each name
once, none over 128 MiB, all together at most 1 GiB, and at most 4096 of
them; from r3: a release's largest file is 64 MiB, r3 unpacks to about
420 MiB in 153 members), each written 0644 (a directory 0755) and owned
by the user, whatever the tarball says; that it is a
complete xz-compressed tar; the manifest's `format` 2 and `release`; the four
image sets, and every other set it lists; each image file at its set's path
with its size and sha256; and each descriptor's paths (inside the release,
files present). Only then is it renamed in, an existing directory of the same
release moved aside; the descriptors are written, each by a temporary file and
a rename; if that fails, each descriptor written is removed (or, for the same
release, put back as it was) and the earlier directory is put back. Then every other `10-axl-firmware-*`
or `20-axl-firmware-*` descriptor there is removed, so only the installed
release is advertised. Other releases' directories stay.

## The installer conformance fixture

`tests/conformance/` is a miniature release in this layout (stand-in images
of a few bytes, a format-2 manifest with every field, four descriptors) and
a set of hostile ones, each with the outcome an installer must give it, in
`expected.json` (its own `format` 1). `tests/conformance/make-fixture.py`
(standard library only) generates every file deterministically, and
`tests/test_conformance.py` holds `install.py` to every case. Another
installer (axlvmpool's, axl-sdk's) vendors the directory and asserts the same
outcomes: `installed` (the release's files, by sha256, and its descriptors,
exactly), `refused` (a stable `reason` and `stage` per case, and install.py's
own message, with the prefix left as it was) or `waits` (for another
install's lock on `<prefix>/.lock`). DESIGN.md §4.35.

## Example

`manifest.json` from CI run 45650143026 (main at `fa45456`, a candidate for
`edk2-stable202611-r1`; it will be replaced by the published release's). Cut
for length: `images` shows only `x64` (the other three sets have the same
fields, AARCH64's `measured` adding `cpu_models_reach_shell`),
`verified.drivers` its first two of 24, and `licenses` its first two of 11.
r1 had no `images.<dir>.licenses`: the x64 entry's is r2's, added by hand;
and its `hardware_images` is r3's, also added by hand.
That run predates `edid=off` on the GOP checks, so its 1280x800 sizes were
measured with QEMU's EDID on, and the bounded patch 0003, so that patch's
`sha256` differs from the current file's.

```json
{
  "format": 2,
  "release": "edk2-stable202611-r1",
  "edk2": {
    "tag": "edk2-stable202611",
    "commit": "556dcfb466e9f05affeaff881d738807f8156d3c",
    "url": "https://github.com/tianocore/edk2.git",
    "fixes": [
      "8ba02634ecaa6ff5d0edcea266198f42eca90c53",
      "b8df7d9c8e1e370b862890ebda677278c4c6155f"
    ],
    "patches": [
      {"name": "0001-pointer-drivers.patch", "sha256": "1121bcd28ce9f87c679b66e956784b11d03ce6201c2b96649c74186c9b19dc48"},
      {"name": "0002-bochs-display-video.patch", "sha256": "c87d38788d80122b94b48da0198d97296d3ccfe641d55c842110863a0609c5a7"},
      {"name": "0003-map-debug-uart.patch", "sha256": "39f64a2139a110680af8b86ac102c3d7ca6fb4fc21ad72f310952627472a70cf"}
    ]
  },
  "flags_from": "QEMU v10.0.0 roms/edk2-build.config, plus edk2-stable202408's front page (PcdBootManagerMenuFile, PcdSetupConOut*, PcdSetupVideo*)",
  "built": "2026-11-25T16:25:22Z",
  "built_by": {
    "how": "github-actions https://github.com/aximcode/axl-firmware/actions/runs/45650143026",
    "axl_firmware_commit": "fa45456bc2baacedfeb8c5c991327e4a839e9ab2",
    "uncommitted_changes": 0
  },
  "toolchain": "GCC",
  "compilers": {
    "x64": "gcc 15.2.0",
    "aarch64": "aarch64-linux-gnu-gcc 15.2.0"
  },
  "default_flavour": "DEBUG",
  "hardware_images": {
    "x64": "release/x64",
    "aarch64": "release/aarch64"
  },
  "images": {
    "x64": {
      "arch": "x64",
      "flavour": "DEBUG",
      "machine": "q35",
      "platform": "OvmfPkg/OvmfPkgX64.dsc",
      "defines": [
        "NETWORK_HTTP_BOOT_ENABLE=TRUE",
        "NETWORK_IP6_ENABLE=TRUE",
        "NETWORK_TLS_ENABLE=TRUE",
        "NETWORK_ISCSI_ENABLE=TRUE",
        "NETWORK_ALLOW_HTTP_CONNECTIONS=TRUE",
        "TPM2_ENABLE=TRUE",
        "TPM2_CONFIG_ENABLE=TRUE",
        "TPM1_ENABLE=TRUE",
        "CAVIUM_ERRATUM_27456=TRUE"
      ],
      "pcds": [
        "PcdFirmwareVersionString=Ledk2-stable202611\\0",
        "PcdFirmwareReleaseDateString=L11/17/2026\\0",
        "PcdBootManagerMenuFile=H{0x21,0xaa,0x2c,0x46,0x14,0x76,0x03,0x45,0x83,0x6e,0x8a,0xb6,0xf4,0x66,0x23,0x31}",
        "PcdSetupConOutColumn=80",
        "PcdSetupConOutRow=25",
        "PcdSetupVideoHorizontalResolution=640",
        "PcdSetupVideoVerticalResolution=480"
      ],
      "code": {
        "path": "x64/code.fd",
        "size": 3653632,
        "sha256": "4b4cb8b16800ce7b5da6efa996ebad133b81f023ec5758a3622064facdd76264"
      },
      "vars": {
        "path": "x64/vars.fd",
        "size": 540672,
        "sha256": "5d2ac383371b408398accee7ec27c8c09ea5b74a0de0ceea6513388b15be5d1e"
      },
      "shell": {
        "path": "x64/Shell_X64.efi",
        "size": 995328,
        "sha256": "0f9b28636e939bd7b45aec14a4ea41d44949665abd7d87bf6b4d9324747dbac4"
      },
      "descriptor": "descriptors/10-axl-firmware-edk2-stable202611-r1-x64.json",
      "debug": {
        "target": "DEBUG",
        "asserts_compiled": true,
        "property_mask": "0x2f",
        "print_error_level": "0x8000004f",
        "boot_manager_menu": "UiApp",
        "assert": "deadloop",
        "clear_memory": true,
        "output": "debugcon I/O port 0x402",
        "descriptor_feature": "verbose-dynamic"
      },
      "secure_boot": false,
      "verified": {
        "by": "uefi_firmware 1.17 (lib/axlfw/fv.py)",
        "drivers": [
          {"name": "VirtioFsDxe", "guid": "7BD9DDF7-8B83-488E-AEC9-24C78610289C"},
          {"name": "VirtioGpuDxe", "guid": "D6099B94-CD97-4CC5-8714-7F6312701A8A"}
        ],
        "absent": [
          {"name": "SecureBootConfigDxe", "guid": "F0E6A44F-7195-41C3-AC64-54F202CD0A21"}
        ]
      },
      "measured": {
        "text_modes": {
          "serial": {
            "modes": [[80, 25], [80, 50], [100, 31], [128, 40], [160, 42], [240, 56]],
            "current": [240, 56]
          },
          "std-vga": {
            "modes": [[80, 25], [100, 31], [128, 40], [160, 42]],
            "current": [160, 42]
          },
          "virtio-gpu": {
            "modes": [[80, 25], [100, 31], [128, 40], [160, 42]],
            "current": [160, 42]
          },
          "bochs-display": {
            "modes": [[80, 25], [100, 31], [128, 40], [160, 42]],
            "current": [160, 42]
          }
        },
        "smbios_bios_version": "edk2-stable202611",
        "nv_after_warm_reset": true,
        "debug_output": {
          "where": "debugcon 0x402",
          "bytes": 227332,
          "info_lines": 246
        },
        "nv_after_cold_start": true,
        "gop_std_vga": {
          "size": [1280, 800],
          "drawn": true
        },
        "front_page_after_exit": true,
        "bochs_display": {
          "devices": 1,
          "size": [1280, 800],
          "drawn": true
        }
      },
      "licenses": [
        {"path": "LICENSES/edk2-License.txt", "chosen": true},
        {"path": "LICENSES/CryptoPkg_Library_OpensslLib_openssl/LICENSE.txt", "chosen": true}
      ]
    }
  },
  "descriptors": [
    "descriptors/10-axl-firmware-edk2-stable202611-r1-x64.json",
    "descriptors/10-axl-firmware-edk2-stable202611-r1-aarch64.json",
    "descriptors/20-axl-firmware-edk2-stable202611-r1-release-x64.json",
    "descriptors/20-axl-firmware-edk2-stable202611-r1-release-aarch64.json"
  ],
  "symbols": {
    "x64": "axl-firmware-edk2-stable202611-r1-symbols-x64.tar.xz",
    "aarch64": "axl-firmware-edk2-stable202611-r1-symbols-aarch64.tar.xz"
  },
  "licenses": [
    "LICENSES/edk2-License.txt",
    "LICENSES/CryptoPkg_Library_MbedTlsLib_mbedtls/LICENSE"
  ]
}
```
