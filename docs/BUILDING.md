# Building from source

Releases are built by CI (`.github/workflows/ci.yml`); nobody needs to build
edk2 to use them. Build locally to change the firmware, or to check a release
against its source.

## Prerequisites

| Need | Fedora / AlmaLinux | Debian / Ubuntu |
|---|---|---|
| git, make, gcc, xz, flock | `git make gcc xz util-linux` | `git build-essential xz-utils util-linux` |
| libuuid's headers (BaseTools) | `libuuid-devel` | `uuid-dev` |
| nasm, iasl | `nasm acpica-tools` | `nasm acpica-tools` |
| an AArch64 cross gcc | `gcc-aarch64-linux-gnu` | `gcc-aarch64-linux-gnu` |
| python3 3.9+ with venv (3.10+ for uefi_firmware's prebuilt wheel) | `python3` | `python3 python3-venv` |
| QEMU, for the boot checks | `qemu-system-x86 qemu-system-aarch64`, or a source build | `qemu-system-x86 qemu-system-arm` |

`build.sh` checks each before it starts. `GCC_AARCH64_PREFIX` names another
cross prefix; `QEMU_DIR` names a directory holding `qemu-system-x86_64` and
`qemu-system-aarch64` (for example a source build's `$HOME/projects/qemu/install/bin`);
`JOBS` (default 4) is the build's parallelism. The build runs at nice 19.

The checks' Python runs in a venv that `build.sh` makes once in
`$XDG_CACHE_HOME/axl-firmware/venv-<digest of requirements.txt>`, installing
`requirements.txt` with `--require-hashes` (uefi_firmware 1.17 only). The
install command needs none of it.

## A build

```sh
./build.sh build                 # fetch (once), build, check, pack into dist/
./build.sh build --install       # and install dist/'s tarball
./build.sh build --edk2 DIR      # build against DIR instead of the cache
```

Steps, in order:

1. **The source.** By default `$XDG_CACHE_HOME/axl-firmware/edk2/<tag>`
   (`build.sh fetch` does only this step):
   a blob-less clone of the tag (`--filter=blob:none --single-branch`), with
   every submodule but `UnitTestFrameworkPkg`'s (`--depth 1`), fetched into a
   temporary directory and renamed in, then made read-only. `EDK2_SRC=<clone>`
   seeds it from a local clone instead. A cache is reused only while
   `build.sh check-source` passes on it: HEAD and the tag are the pinned commit;
   no entry is hidden from `git status` (skip-worktree, assume-unchanged); no
   tracked file changed and no untracked file that is not ignored; each
   submodule at its recorded commit; each required fix (`fixes` in `build.sh`:
   the VirtioRng fix `8ba02634` and the LPA2 fix `b8df7d9c`) an ancestor.
   `--edk2 DIR` takes a tree that passes the same check. A tree's ignored files
   (a `Build/` or `Conf/` of its own) are allowed: the build never reads them.
2. **The workspace**, `work/ws` in this checkout, made afresh:
   - `BaseTools/`: a copy of the source's tracked BaseTools files (submodules
     included), built there with `make -C BaseTools`. **No in-tree BaseTools
     step is needed:** the C tools and their Python run from the copy.
   - `overlay/<Package>/`: a copy of each package a patch touches (today
     `ArmVirtPkg` and `OvmfPkg`), with `patches/*.patch` applied in name order
     by `git apply`.
   - `Conf/` and `Build/`: edk2's, from `$EDK_TOOLS_PATH/BuildEnv`.
   - The environment: `WORKSPACE=work/ws`, `PACKAGES_PATH=work/ws/overlay:<source>`,
     `EDK_TOOLS_PATH=work/ws/BaseTools`, `CONF_PATH=work/ws/Conf`. edk2 resolves
     each package path in `PACKAGES_PATH` order, so the patched copies shadow
     the source's, and every other package comes from the source unchanged.
3. **Four builds**: `OvmfPkg/OvmfPkgX64.dsc` (`-a X64`) and
   `ArmVirtPkg/ArmVirtQemu.dsc` (`-a AARCH64`), each DEBUG and RELEASE, with
   QEMU v10.0.0's flags, the front-page PCDs, and the version string and date
   ([DESIGN.md](DESIGN.md) §4). Each build's log is `work/ws/build-<ARCH>-<TARGET>.log`.
4. **The source is unchanged**: `git status --porcelain --ignored` of the tree
   and its submodules must say the same before and after, or the build fails.
5. **Assemble** (`lib/axlfw/release.py`): stage the images (AARCH64's padded to
   64 MiB), run the static checks, then the boot checks (two image sets at a
   time, `AXLFW_BOOT_JOBS`; each `expect` waits up to `AXLFW_BOOT_TIMEOUT`,
   180 s by default; console transcripts in `work/stage/boot-logs/`), write the
   licences, descriptors and manifest, and pack `dist/`. Any failed check stops
   it before anything is packed. [PROPERTIES.md](PROPERTIES.md) lists the checks.

Measured on AlmaLinux 10 x86_64 (gcc 14, 4 jobs, nice 19), in axlvmpool: about
1 min 20 s per arch and flavour from clean. The boot checks take about 25 s for
X64 under KVM and a few minutes for AARCH64 under TCG.

## CI

`.github/workflows/ci.yml`, on GitHub-hosted `ubuntu-latest`:

- **lint** (every push and pull request): `shellcheck`, `pyright` (strict), and
  `tests/firmware-tests.sh`.
- **build**: Ubuntu's packages (above), KVM enabled when the runner has
  `/dev/kvm`, the edk2 source cached by its pinned commit (`actions/cache`),
  `./build.sh build`, then the no-build tests again with the source and an
  install of the build, then `./build.sh check` on the tarball unpacked as a
  consumer would. The release (`dist/`) and the boot transcripts are kept as
  artifacts.
- **release**, on a tag `edk2-stable*-r*`: the tag must equal `build.sh`'s
  `tag` and `revision`; `SHA256SUMS` is checked; `gh release create` publishes
  the three tarballs and `SHA256SUMS`, with the tag's `CHANGELOG.md` section as
  notes.

Actions are pinned to full commit SHAs.

## Moving to a new edk2 tag

Followed for 202602 to 202611 (2026-11-25); [DESIGN.md](DESIGN.md) §4.1 has
what was found.

1. **Pin it**: `tag`, `commit` and `revision=1` in `build.sh` (and any new
   required fix in `fixes`); `EDK2_TAG` and `EDK2_COMMIT` in `ci.yml`; `tag`
   and `commit` in `tests/firmware-tests.sh`; the tag and commit in
   `tests/fixtures/descriptors.json` and `tests/test_axlfw.py`. Then
   `./build.sh fetch` fetches the new source into its own cache directory
   (the old one is left alone) and checks it, the fixes included.
2. **Read what changed** between the tags (`git log <old>..<new> -- <path>` in
   the cache) in `OvmfPkg`, `ArmVirtPkg`, `MdeModulePkg/Universal/BdsDxe` and
   `Console`, and `ShellPkg`'s `mode`, `dmpstore` and `smbiosview` (their
   output is parsed); and in the source, each assumption the build rests on:
   `PcdBootManagerMenuFile` in `OvmfPkgX64.dsc` and `ArmVirtQemu.dsc` (still
   BootManagerMenuApp?); `OvmfDisplayPcds.dsc.inc`; TerminalDxe's mode table;
   the Shell's boot option category (`LOAD_OPTION_CATEGORY_APP`); every `-D`
   define of QEMU's flags still referenced; `OUTPUT_DIRECTORY`; the
   `PcdDebugPropertyMask` and `PcdDebugPrintErrorLevel` values; whether
   upstream now has anything a patch adds. Record each finding in DESIGN.md.
3. **Re-check, then rebase, each patch** (all three: 0001 pointer drivers,
   0002 bochs-display video, 0003 the debug UART's PEI mapping). First check
   whether upstream has fixed or added what it does by the new tag; if so,
   drop the patch (or the part), and say so in CHANGELOG.md and DESIGN.md.
   Then rebase what remains: copy the tracked files of the packages they touch
   (`git ls-files -z -- OvmfPkg ArmVirtPkg | tar ...`), apply the patches in
   order with `git apply` (resolve by hand where one fails), and regenerate
   each from the copies before and after it with `git diff --no-index`, keeping
   its header's prose (with the new tag named). The edk2 files are CRLF;
   `.gitattributes` keeps the patches byte-exact.
4. **Re-check the GUIDs**: `tests/firmware-tests.sh` (or
   `AXL_FIRMWARE_EDK2=<tree>`) compares every checked GUID with its INF at the
   new tag.
5. **Build**: `./build.sh build`. Every property is checked; fix what fails, and
   record why in DESIGN.md.
6. **Re-measure the text modes**: the build measures them; compare the new
   manifest's `text_modes` with the last release's, and say any change in
   `CHANGELOG.md` (consumers' launchers read them).
7. **Release**: a `CHANGELOG.md` section `## <tag>-r1`, push, let CI pass, then
   push the tag. A rebuild of the same edk2 tag (a new patch, a flag) is the
   next `-r<N>`: bump `revision`.
8. **Tell the consumers** (axlvmpool, axl-sdk, SoftBMC, AGT, the downstream
   diagnostics project): the new tag, its `SHA256SUMS`, and what changed. Each
   moves its pinned tag and sha256 when ready ([CONSUMERS.md](CONSUMERS.md)).
   A change to the release format is agreed with them first.
