# axl-firmware: design and decisions

This is the project's spec and its decision record. A change of behaviour, or
of a decision, is made here in the same commit. Section 4 lists decisions
already taken: they are not re-opened without the owner. Section 5 lists the
owner's open decisions, each with a recommendation. Section 6 lists the
assumptions still to verify, and how each verified one was verified.

The decisions were taken in axlvmpool, whose `docs/DESIGN.md` §11 items 101 and
102 hold the full discussion (2026-11-24/25). This repository's history starts
from axlvmpool commit `4ad98ed`, where `guest/build-firmware.sh` and
`tools/firmware-tests.sh` became `build.sh` and `tests/firmware-tests.sh`.

## 1. Why this exists

Running a UEFI guest needs firmware images (OVMF, AAVMF): runtime files, not an
edk2 build dependency. The aximcode projects need, from those images:

- **VirtioFsDxe** (`7BD9DDF7-8B83-488E-AEC9-24C78610289C`): a guest's shared
  directory. Without it, axl-sdk's `test-pool-guest` failed three cases per arch.
- **VirtioGpuDxe** (`D6099B94-CD97-4CC5-8714-7F6312701A8A`): the GPU classes.
- **edk2's VirtioRng fix** `8ba02634ecaa6ff5d0edcea266198f42eca90c53`
  ("OvmfPkg/VirtioRngDxe: protect VirtioRngGetRNG against timer callbacks"):
  without it a boot can stall (axlvmpool item 99). `edk2-stable202602` is the
  first stable tag after it.
- **AARCH64 booting on any CPU model QEMU offers**, `-cpu max` included: edk2
  202511 to 202602 hang there (§4.1).
- **Pointer drivers** (AGT's GUI, a downstream diagnostics project's cursor) and
  **video on AARCH64's `bochs-display`** (that project's video test).

No downloadable image has all of these (measured 2026-11-25 on AlmaLinux 10
x86_64 with axl-sdk's `fwtool`, then this repository's walker):

| Image | VirtioFsDxe | VirtioGpuDxe | VirtioRng fix |
|---|---|---|---|
| QEMU 10.0.0 to 11.1.0's bundled `edk2-*-code.fd` (edk2-stable202408) | yes | yes | no |
| AlmaLinux edk2 20260221 `OVMF_CODE.fd` | no | no | yes |
| AlmaLinux edk2 20260221 `QEMU_EFI-pflash.raw` | no | yes | yes |

So the firmware is built here, once, in CI, and every project installs the
release. Nobody else builds edk2. Building from source stays possible for the
firmware's maintainer, or anyone who wants to.

## 2. Prior art, and what was borrowed

| Project | What it offers | Verdict here |
|---|---|---|
| rust-osdev/ovmf-prebuilt | CI builds each edk2 stable tag; one release tarball per tag (`edk2-stable202602-r1`); its crate fetches by tag and checks a pinned sha256 | **The mechanism, borrowed:** release per tag, `-r<N>` revisions, sha256 pinning. Its images lack the front-page PCD (§4.4) and use other flags, so they are not used. |
| QEMU's `roms/edk2-build.py` and `edk2-build.config` | The flags QEMU builds its bundled `pc-bios/edk2-*.fd` with | **The flags, reproduced** from QEMU v10.0.0's config, not vendored: `edk2-build.py` has no licence header, so it falls under QEMU's GPL-2.0. QEMU's own images are stuck at stable202408. |
| retrage/edk2-nightly | Unofficial nightly builds | Same shape; moves too fast to pin. |
| Distribution packages (AlmaLinux `edk2-ovmf`, Debian `ovmf`) | Maintained images and descriptors | Lack VirtioFsDxe (and on X64 VirtioGpuDxe). Their descriptors (`/usr/share/qemu/firmware`) are the model for ours. |
| libvirt | Firmware chosen through QEMU's firmware descriptors (`docs/interop/firmware.json`) | **The contract, borrowed:** this release ships descriptors, and its consumers choose firmware through them. |

## 3. Scope

In: building, checking, packing and publishing the images; an install command;
the descriptors. Out: choosing a firmware per guest (each consumer's own
discovery, for example axlvmpool's `axlvmpool firmware`), and any QEMU change.

## 4. Decisions taken

1. **The pinned tag** (owner, 2026-11-25): `edk2-stable202611`, commit
   `556dcfb466e9f05affeaff881d738807f8156d3c`, the latest stable tag. It
   replaced `edk2-stable202602` (`b7a715f7`) before the first release, which
   was held for this:
   - *The LPA2 hang.* AARCH64 builds of 202602 stop after the version banner
     on a CPU with FEAT_LPA2, such as QEMU's `-cpu max`; `max,lpa2=off`,
     `cortex-a57` and `neoverse-v1` boot, as does QEMU's bundled 202408 under
     `max`. It is edk2 issue #11962, introduced by `90771630bfe1`
     ("UefiCpuPkg/ArmMmuLib: Add support for LPA2", first in 202511) and fixed
     by `b8df7d9c8e1e370b862890ebda677278c4c6155f` ("ArmVirtPkg/ArmPlatformLibQemu:
     Support early ID map on LPA2 capable CPUs", in 202605 and later).
     rust-osdev's upstream builds agree: 202508 boots, 202511 and 202602 hang,
     202605 boots. An investigator verified the fix with these flags under
     `max`, `max,lpa2=off`, `cortex-a57`, `neoverse-v1`, `neoverse-n1` and
     `cortex-a710`.
   - *Required fixes.* The build checks that both `8ba02634` (VirtioRng) and
     `b8df7d9c` (LPA2) are ancestors of the pin, which guards the pins against
     each other; the manifest lists both (`edk2.fixes`). A new property boots
     AARCH64 under `max`, `cortex-a57` and `neoverse-v1` (PROPERTIES.md).
   - *Re-checked on 202611* (2026-11-25), each finding made on 202602:
     `PcdBootManagerMenuFile` is still BootManagerMenuApp in `OvmfPkgX64.dsc`
     and `ArmVirtQemu.dsc`, so the front-page fix (§4.4) is still needed;
     `OvmfDisplayPcds.dsc.inc` and TerminalDxe's mode table are unchanged;
     the Shell is still a category-APP boot option (§4.6; ArmVirtQemu now uses
     OvmfPkg's `PlatformBootManagerLibLight`); every `-D` define of QEMU's flags
     is still read where it was (`TPM2_CONFIG_ENABLE` by ArmVirtQemu only, as
     before); the output directories are still `Build/OvmfX64` and
     `Build/ArmVirtQemu-AArch64`; `PcdDebugPropertyMask` is still 0x2F
     (OvmfPkgX64) and 0x2f (ArmVirt DEBUG); `PcdVideo*` 1280x800 still comes
     from `OvmfDisplayPcds.dsc.inc`; every checked GUID is still its INF's.
     Upstream added none of the patches' drivers, nor QemuVideoDxe on
     ArmVirtQemu, nor the write-combine PCD, so patches 0001 and 0002 stay
     whole, regenerated against 202611; 202611 needed a third, 0003 (§4.19). Changes met: ArmVirtQemu's components and
     PCDs partly moved into `ArmVirt.dsc.inc`; VirtioKeyboardDxe is now
     VirtioInputDxe; `ArmVirtQemu.dsc` sets `PcdDebugPrintErrorLevel|0` for
     VirtioSerialDxe; ArmVirtQemu may dispatch an emulated variable store
     ("when necessary"), which the NV checks guard. The text modes, the GOP,
     NV persistence and the SMBIOS version are re-measured by every build.
2. **QEMU's flags** (2026-11-25): DEBUG, the GCC toolchain (QEMU 10.0.0 names `GCC5`,
   which edk2-stable202611 removed for its successor `GCC`), and `[opts.common]`
   (`NETWORK_HTTP_BOOT_ENABLE NETWORK_IP6_ENABLE NETWORK_TLS_ENABLE
   NETWORK_ISCSI_ENABLE NETWORK_ALLOW_HTTP_CONNECTIONS TPM2_ENABLE
   TPM2_CONFIG_ENABLE TPM1_ENABLE CAVIUM_ERRATUM_27456`, all TRUE); AARCH64 adds
   `DEBUG_PRINT_ERROR_LEVEL=0x80000000` (errors only),
   `PcdDxeNxMemoryProtectionPolicy=0xC000000000007FD1` and
   `PcdUninstallMemAttrProtocol=TRUE`, and pads code and vars to 64 MiB. So the
   DEBUG images behave as the QEMU-bundled ones consumers booted before: X64
   verbose on the debug port 0x402 (`verbose-dynamic`), AARCH64 errors only on
   the second UART (`verbose-static`, as QEMU's own descriptor labels it).
3. **DEBUG and RELEASE flavours** (2026-11-25, after consulting a downstream
   diagnostics project): RELEASE has the same patches and defines. DEBUG stays
   the default: its descriptors are numbered `10-`, RELEASE's `20-`, so the
   shared descriptor rule (lowest name first) picks DEBUG.
4. **The front page** (2026-11-25): `PcdBootManagerMenuFile` is UiApp
   (`462CAA21-7614-4503-836E-8AB6F4662331`), stable202408's value, on both
   arches. edk2 changed it to BootManagerMenuApp (`9d9e3a2b`, `4e19daa8`), so a
   Shell's `exit` landed in the boot menu instead of the front page, and four
   axlvmpool tests failed. The front page's own modes are restored too:
   `PcdSetupConOutColumn=80`, `PcdSetupConOutRow=25`,
   `PcdSetupVideoHorizontalResolution=640`, `PcdSetupVideoVerticalResolution=480`.
   A build option, not a patch.
5. **The larger text modes are kept** (owner, 2026-11-25): TerminalDxe gained
   128x40, 160x42 and 240x56, and with `PcdConOutColumn`/`Row` at 0 the console
   takes the largest mode its devices share (240x56 on a serial-only console).
   Pinning a size would hide bugs that real hardware's large modes show
   (SoftBMC). The rule for every consumer: a test reads or sets the console size
   (`mode C R`) and never assumes one. Interactive launchers set the guest
   console to the user's terminal. The release states each arch's measured mode
   list per display (`text_modes`), so a launcher can choose without a probe
   boot.
6. **No Shell fallback** (owner, 2026-11-25): since stable202602 the embedded
   Shell is a category-APP boot option, so with no bootable disk BDS says "No
   bootable option or device was found" instead of starting it. No consumer
   relies on the fallback; there is no PCD for it. The Shell is still embedded
   (FFS `7C04A583-9E3E-4F1C-AD65-E05268D0B4D1`) and ships beside each code image
   as `Shell_<ARCH>.efi`.
7. **Patch 0001, pointer drivers** (owner, 2026-11-25): UsbMouseDxe and
   UsbMouseAbsolutePointerDxe on both arches, Ps2MouseDxe on X64. AGT's GUI and
   a downstream project's cursor need a pointer. The first source patch.
8. **Patch 0002, bochs-display video on AARCH64** (owner, 2026-11-25): QemuVideoDxe
   in ArmVirtQemu, `PcdRemapFrameBufferWriteCombine=TRUE` (the framebuffer BAR
   otherwise maps as Device memory, where unaligned pixel writes fault), and
   QemuVideoDxe's legacy VGA register accesses skipped for `bochs-display`. A
   downstream diagnostics project needs a PCI display controller of subclass
   "other" with a linear framebuffer, which on `virt` is `bochs-display`.
   - *The gate, narrowed in review (2026-11-25).* The downstream reference
     skipped the accesses for the whole MMIO variant (`QEMU_VIDEO_BOCHS_MMIO`).
     Upstream gives that variant to std-VGA, secondary-vga and virtio-vga too,
     and reaches their VGA block through BAR2 at 0x400; QEMU blanks a VGA whose
     attribute controller was never unblanked (`vga.c`, `ar_index & 0x20`), so
     std-VGA would have shown nothing. Ours skips them only off x86, for the
     MMIO variant, when the PCI subclass is "other": bochs-display, whose BAR2
     has nothing at 0x400 (unassigned MMIO, which faults on `virt`).
     secondary-vga has the same IDs and subclass, so on AARCH64 it is treated
     the same way (its display may stay blank). X64's driver is upstream's.
     The std-VGA and bochs-display boots check that the screen is drawn.
   - *The bochs-display check without its EDID (2026-11-25).*
     `QemuVideoBochsEdid` reads the EDID for the whole MMIO variant and its
     preferred mode overrides `PcdVideoHorizontalResolution`/`Vertical`, and
     QEMU's default EDID asks 1280x800 too, so a plain `-device bochs-display`
     measured QEMU, not the firmware. QEMU 10.0.0's `bochs-display` has
     `edid=<bool>` (default on; `hw/display/bochs-display.c`, and
     `-device bochs-display,help`), so the check boots `bochs-display,edid=off`,
     as std-VGA's does. Shown on the candidate images of CI run 45653672897
     (AlmaLinux 10 x86_64, QEMU 10.0.0), X64 and AARCH64 alike: plain, 1280x800;
     `xres=1024,yres=768` (the EDID's preferred mode), 1024x768; `edid=off`,
     1280x800; `edid=off,xres=1024,yres=768`, 1280x800. Without the EDID,
     QemuVideoDxe's read of BAR2 does not fault on `virt` (std-VGA's
     `edid=off` boot shows the same on AARCH64).
9. **The firmware version string is the edk2 tag** (2026-11-25): SMBIOS type 0's
   BIOS version reads `edk2-stable202611` on both arches, which a downstream
   project reports. It replaces axlvmpool's `edk2-stable202602-for-axlvmpool`.
10. **Prebuilt, in its own repository** (owner, 2026-11-25): this repository,
    private on GitHub, with CI on GitHub-hosted `ubuntu-latest` (keeping the
    load off the developers' hosts). Releases are tagged `<edk2 tag>-r<N>`.
11. **One shared edk2 tree, out of tree** (owner, 2026-11-25): firmware builds
    use edk2's out-of-tree build (`WORKSPACE` for output, `PACKAGES_PATH` for
    source), so no project writes into or cleans a shared tree. Patches are
    applied to a copy of the packages they touch, first in `PACKAGES_PATH`;
    BaseTools is copied into the workspace and built there
    ([BUILDING.md](BUILDING.md)).
12. **The neutral install path** (owner, 2026-11-25):
    `$XDG_DATA_HOME/axl-firmware/<release>/` and
    `$XDG_DATA_HOME/axl-firmware/descriptors/`, searched first by axlvmpool and
    axl-sdk. Not `$XDG_CONFIG_HOME/qemu/firmware`, which libvirt reads too and
    would switch the user's own VMs to these images.
13. **Descriptors are the shared contract** (2026-11-25): consumers choose
    firmware through QEMU descriptors, by features: interface `uefi`; the arch's
    target and a machine glob matching `q35` or `virt`; flash, split, raw; no
    `secure-boot`, `enrolled-keys`, `requires-smm`, `intel-tdx`, `amd-sev-snp`
    or `host-uefi-vars`; on AARCH64 a `verbose-static` image first; then the
    lowest file name. Descriptors list only the features the build has.
14. **No Secure Boot** (2026-11-25): consumers boot unsigned images.
15. **The FV walker** (2026-11-25): uefi_firmware 1.17 (pip `uefi_firmware`,
    pinned by digest), given each volume found by its `_FVH` signature, since its
    own `AutoParser` misses ArmVirtQemu's image. It names the same FFS files as
    axl-sdk's `fwtool` on every image tried (§6), and needs nothing private, so
    CI can run it.
16. **The network stack, per arch** (coordinator, 2026-11-25): SoftBMC's need
    is the SNP *protocol*. ArmVirtQemu does not build SnpDxe (it serves UNDI
    option ROMs); there VirtioNetDxe produces EFI_SIMPLE_NETWORK_PROTOCOL
    itself. So the network stack checked is SnpDxe, MnpDxe, Ip4Dxe, TcpDxe and
    Dhcp4Dxe on X64, and VirtioNetDxe, MnpDxe, Ip4Dxe, TcpDxe and Dhcp4Dxe on
    AARCH64 (VirtioNetDxe is checked on both).
17. **The debug OVMF is the default X64 image** (coordinator, 2026-11-25): DEBUG,
    `PcdDebugPropertyMask` 0x2F (ASSERT with deadloop, CLEAR_MEMORY), verbose on
    0x402, stated in the manifest (`images.x64.debug`). axl-sdk's
    `find_debug_ovmf`, which looks for `OVMF_CODE.fd` and `OVMF_VARS.fd`, adapts
    to the release layout on its side; the release adds no alias file names.
18. **AARCH64 secondary-vga may stay blank** (coordinator, 2026-11-25, accepted
    as a known limitation): patch 0002 skips the legacy VGA accesses for any
    MMIO-variant display of subclass "other" off x86, and secondary-vga shares
    bochs-display's IDs and subclass, so its attribute controller is never
    unblanked. Nobody uses it on AARCH64; std-VGA staying correct matters more.
19. **Patch 0003, the debug UART mapped on AARCH64** (owner, 2026-11-25: carry it,
    with no upstream report). Found by the first CI build of 202611: an AARCH64 DEBUG
    image with two UARTs (QEMU's `-serial` twice, as axl-sdk's
    `qemu_fw_debug_uart` and the boot checks run it) took "Synchronous
    Exception" after the banner and never booted; RELEASE, and DEBUG with one
    UART, booted. Reproduced on AlmaLinux 10 x86_64 with QEMU 10.0.0 under
    `cortex-a57` and `max`: a level 3 translation fault (ESR 0x96000007) at FAR
    0x9040030, the second PL011's UARTCR, in PeiCore's `MmioRead32` (the DEBUG
    write). The cause is upstream `158c1b614` ("ArmVirtPkg: Refactor virtual
    memory mapping for qemu guest firmware", 2026-08-28, first in 202611):
    PEI maps only the console UART's page and fw_cfg, no longer `virt`'s
    peripheral window, while `DebugLibFdtPL011Uart` writes to the *other*
    PL011. No upstream fix or issue as of 2026-11-25. The patch maps every
    PL011 in the device tree as the console's is mapped (device memory, CCA
    bit). Verified: with it, both CPUs boot with two UARTs, the second
    carrying errors only (687 bytes, no INFO line); without it, the same build
    faults. *Bounded (2026-11-25, review):* `FdtSerialGetPorts` returns at most
    two PL011s (`FDT_SERIAL_PORTS.BaseAddress[2]`), and when "/chosen" names a
    third as the console, neither is the console's page, so the table needs
    DRAM, the FV, the console UART, two more UARTs, fw_cfg and the end: 7
    entries. Upstream's 5 plus one (the first version) would have written the
    seventh past a 6-entry pool. The table is now 7, and the loop also stops
    with two entries left for fw_cfg and the end. Rebuilt (AARCH64 DEBUG) and
    booted on AlmaLinux 10 x86_64 with QEMU 10.0.0 under `cortex-a57` with two
    UARTs: the Shell reached, 664 bytes on the second UART, no INFO line. The owner decided on 2026-11-25 to carry it here and not to report
    it upstream; each tag move re-checks whether upstream has fixed it
    (BUILDING.md). Every release checks it: the AARCH64 serial boot runs with
    two PL011s, must reach the Shell, and the second UART must carry
    errors-only DEBUG output (PROPERTIES.md; RED on the unpatched build).

## 5. Open decisions (the owner's)

1. **Release signing.** A release is checked by sha256 only. GitHub's artifact
   attestations could sign what CI built. *Recommendation:* add when a consumer
   outside the organisation fetches releases.
2. **AARCH64 reproducibility.** axlvmpool found X64 byte-reproducible and
   AARCH64 not (every build differed). *Recommendation:* look into it before
   anyone relies on rebuilding a release byte for byte; until then a digest
   names a build, not the source.
3. **RELEASE symbols.** Only DEBUG's symbols are published.
   *Recommendation:* add RELEASE's when a consumer debugs a RELEASE image.
4. **An uninstall command.** None yet; the install layout makes one simple
   (remove `<prefix>/<release>` and its descriptors).
5. **QEMU's flags reproduced rather than vendored** (decided by the builder in
   axlvmpool item 101; the owner may revisit).

## 6. Assumptions

Verified:

- *uefi_firmware names the same FFS files as fwtool.* Verified 2026-11-25 on
  axlvmpool's stable202602 X64 (143 files) and AARCH64 (112) images, QEMU
  10.0.0's bundled AARCH64 image (111), AlmaLinux's `OVMF_CODE.fd` (135) and
  `QEMU_EFI-pflash.raw` (108): the same GUIDs, plus the free-space pseudo-file,
  which the walker drops. Its `AutoParser` found nothing in either AARCH64 image.
- *The checked GUIDs are the INFs'.* `build.sh`'s tests compare each with its
  INF (or `ShellPkg.dec`'s `gUefiShellFileGuid`) at the tag.
- *The build writes nothing into the source.* The fetched source is read-only,
  and `build.sh` compares `git status --porcelain --ignored` of the tree and its
  submodules before and after.
- *QEMU's `bochs-display` takes `edid=off`.* QEMU 10.0.0's
  `hw/display/bochs-display.c` defines `DEFINE_PROP_BOOL("edid", ...,
  true)`, and `-device bochs-display,help` lists `edid=<bool> - on/off
  (default: on)`; the boots in §4.8 show the size then follows the firmware.
- *AARCH64 202602 under `-cpu max` hangs after the version banner* (seen here
  2026-11-25 with axlvmpool's stable202602 image: no Shell within 60 s, where
  `cortex-a57` reached it in 11 s; the same image is the CPU check's control).
  Cause and fix: §4.1. The other boot checks use `cortex-a57`, axlvmpool's
  default.

To verify:

- *GitHub-hosted runners expose KVM.* The CI enables `/dev/kvm` when present;
  otherwise X64 boots under TCG, which is slower but equivalent.
- *The Shell's `mode`, `dmpstore` and `smbiosview` output formats* are those of
  ShellPkg's at the pin (`STR_MODE_LIST_ITEM` and so on); a new tag must be
  re-checked ([BUILDING.md](BUILDING.md), "Moving to a new edk2 tag").
- *AARCH64's DEBUG output always holds an error line* (Tcg2's "Not Found" with
  no TPM attached), which the flavour check relies on; a tag that silences it
  needs the check adjusted.
- *Ubuntu's compilers build the same behaviour as AlmaLinux's* (gcc 13 against
  14): the property checks are the guard; the bytes differ.
