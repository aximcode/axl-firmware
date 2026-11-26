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
     whole, regenerated against 202611; 202611 needed a third, 0003 (§4.19),
     and a fourth, 0004 (§4.21), in r2; r3 adds a fifth, 0005 (§4.26), and
     0006-0010 (§4.28). Changes met: ArmVirtQemu's components and
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
    on GitHub (private at first; **public from 2026-11-25**, decision 20), with CI on GitHub-hosted `ubuntu-latest` (keeping the
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
20. **Public, with releases here** (owner, 2026-11-25, after consulting the
    consumers). Private releases need a token, which axl-sdk's installer
    (plain curl plus a pinned sha256, no token support) and a downstream
    project's users behind a TLS-intercepting proxy do not have. Publishing the
    firmware into the public `axl-sdk-releases` instead was weighed and
    rejected: by default GitHub makes the newest release "Latest", and the SDK's
    documented install (`…/axl-sdk-releases/releases/latest/download/install.sh`)
    and its release checks read Latest, so one firmware release without
    `make_latest: false` would break every new SDK install; a firmware tag there
    would also point at an SDK snapshot commit, making GitHub's source archive
    the SDK's. Here, Latest means the newest firmware, the source archive is
    this repository's, and no cross-repository write token is needed. Before
    the change the history, the release and the CI artifacts were audited: no
    host names, home paths, LAN IPs or vendor names; the release records only
    its CI run, commit and compilers. The install now falls back to the plain
    release URL with no login (sha256 is the trust anchor).
21. **Patch 0004, serial control keys reach key notifications** (owner,
    2026-11-25: carry it, with no upstream report). Found by axl-sdk on r1,
    both arches: a serial Ctrl-C behind typed-ahead keys brought the Shell's
    prompt back only after 40-49 s, and did not stop a running command
    part-way; every axl-sdk test that types a Ctrl-C was green on 202602.
    The cause is upstream `184fb6b1035e` ("MdeModulePkg/TerminalDxe: Fix
    handling of shift state in notifications", 2026-05-26, issue #12281,
    first in 202608): TerminalDxe's `IsKeyRegistered` now requires a
    registration's KeyShiftState to equal the input key's, ignoring only
    `EFI_SHIFT_STATE_VALID`. TerminalDxe gives every key it reads shift state
    0 (`EfiKeyFiFoInsertOneKey` and `KeyNotifyProcessHandler`, the two
    notification paths), while the Shell registers its break as 0x03 and
    'c', and its pause as 0x13 and 's', each with
    `EFI_LEFT_CONTROL_PRESSED` or `EFI_RIGHT_CONTROL_PRESSED` (ShellPkg
    `ShellProtocol.c` and `Shell.c`), through ConSplitter, which passes the
    registration to TerminalDxe unchanged. So 0x03 matched nothing.
    Upstream chose this knowingly: the commit message, and a comment in
    `TerminalConInRegisterKeyNotify`, say that notifications with a shift
    state "will never trigger" over a serial line. No upstream change to
    the Shell's registrations, or report of the break, as of 2026-11-25.
    - *The patch.* In `IsKeyRegistered`: when the input key carries no shift
      state (0 or `EFI_SHIFT_STATE_VALID` alone) and its UnicodeChar is a
      control character 0x01-0x1A other than Backspace, Tab, LF and CR
      (0x08, 0x09, 0x0A, 0x0D), a registration whose shift state is only
      Control bits (left, right or both, with or without VALID) matches.
      Otherwise #12281's exact comparison stands (RCtrl+n does not fire on
      a plain 'n'), and the toggle state is compared exactly as before. The
      ScanCode and UnicodeChar must still be equal, so the Shell's 'c' and
      's' registrations never match serial input. Each serial 0x03 calls
      the Shell's function twice (its LCtrl and RCtrl registrations), as on
      202602. The patch also corrects upstream's comment in
      `TerminalConInRegisterKeyNotify`.
    - *BS, TAB, LF and CR excluded* (owner, 2026-11-25, before r2 was
      published). The first r2 build matched the whole range 0x01-0x1A, so
      a Ctrl+H, Ctrl+I, Ctrl+J or Ctrl+M registration fired on a serial
      Backspace, Tab or Enter. axl-sdk showed the harm: plain typing with
      Enter reached a stale Ctrl registration left on a keyboard device
      that had gone, and its function ran from freed memory, a CPU
      exception with no guard of its own. A terminal sends those four
      characters for keys of their own, not for Ctrl chords, so they keep
      #12281's exact match; Ctrl-C (0x03) and Ctrl-S (0x13) are unaffected.
      *Checked* by compiling the patched `IsKeyRegistered` on the host
      (`tests/firmware-tests.sh`, `tests/terminal-keys/`): it is cut from
      the patched `TerminalConIn.c` at the pin and fed every character
      0x00-0x7F with no shift state against a registration of the same
      character with left, right or both Control bits, which must match
      exactly 0x01-0x07, 0x0B, 0x0C and 0x0E-0x1A; with Shift or Alt
      added, nothing; plus #12281's cases (RCtrl+n against a plain 'n',
      a toggle state that differs, an input with Shift). A boot check of
      Enter against a Ctrl+M registration would need a guest application
      that registers one, which the release does not have; the host test
      runs the same C. RED on the first r2 patch (it matched 0x08, 0x09,
      0x0A and 0x0D too); on upstream's unpatched function it matches
      nothing. The `ctrl-c` boot check stays as it was.
    - *Why TerminalDxe, not ShellPkg.* Registering 0x03 and 0x13 with no
      shift state in the Shell would fix only the Shell this release ships.
      The TerminalDxe patch serves every registrant of a Ctrl key: any
      Shell binary a consumer boots, and applications such as axl-sdk's,
      which also stopped seeing a serial Ctrl-C on r1.
    - *Duplicate registrations.* `RegisterKeyNotify`'s duplicate check calls
      the same function, with the caller's key data: a new registration of
      a control character with no shift state, for a function already
      registered for it with Control held, gets the existing handle, as
      before `184fb6b1035e`. ConSplitter's own check does not merge them, so
      it then holds two entries over one TerminalDxe notification:
      unregistering either removes it, and the other's unregister fails.
      Nothing in edk2 or the Shell registers that way.
    - *Measured* (AlmaLinux 10 x86_64, QEMU 10.0.0; X64 under KVM, AARCH64
      under TCG `cortex-a57`): a Shell script ticking every 0.1 s for a
      minute, sent 0x03 after its third tick. On r1 the script ran to its
      end on all four image sets (X64 DEBUG prompt 52-60 s after the 0x03,
      AARCH64 DEBUG 61.5 s after the script started); on axlvmpool's 202602
      X64 image the prompt came 0.10 s after it; with 0004, 0.10 s on all
      four image sets, three breaks each (the script's 0.1 s `stall` is the
      granularity).
    - *Ctrl-S is not fixed, and was never a serial feature.* The Shell's
      notification function sets its pause flag only for 's' with Control
      held, and the pause loop in `ConsoleLogger.c` re-pauses only on that
      key too; 0x13 is never acted on. Measured: output did not pause after
      a serial 0x13 on axlvmpool's 202602 X64 image, on r1, or with 0004
      (49-50 ticks in the 5 s after it on all four image sets). Making
      serial Ctrl-S pause would need a ShellPkg change: §5.6, done in r3 by
      patch 0007 (§4.28).
    - *Checked on every release*: the `ctrl-c` boot (PROPERTIES.md), RED on
      r1's four installed image sets.

22. **A licence list per image** (owner, 2026-11-25, for r2; asked by a
    downstream consumer that vendors only some images, for example
    `release/x64` and `release/aarch64`, and copies only the licence texts
    they need). Each `images.<dir>` gains `licenses`: a list of `{path,
    chosen}` (RELEASE-FORMAT.md). Format stays 2: a field is added, none
    changed, and a consumer that does not read it is unaffected.
    - *Derived from the build, not by hand.* `build.sh` passes `-y
      <ws>/report-<ARCH>-<TARGET>.txt -Y LIBRARY -Y FLASH` to every
      `build`. Its FLASH section lists the modules in each volume of each
      FD, nested volumes after their parents (X64's `OVMF_CODE`:
      FVMAIN_COMPACT, then PEIFV and DXEFV inside it, and SECFV;
      AARCH64's `QEMU_EFI`: FVMAIN_COMPACT, then FVMAIN inside it); its
      LIBRARY section the library instances linked into each module. A
      part (an edk2 submodule) is in an image when a module in its code
      FD, or a library linked into it, lists a source inside the
      submodule in its INF's `[Sources]`, `[Sources.common]` or
      `[Sources.<ARCH>]`, `[Defines]` DEFINEs expanded, entries for
      another tool family than GCC left out. The INFs are read where
      `build` reads them (the patched overlay first, then the source).
      Nothing maps a part to a library by hand: at the pin the INFs that
      reach a submodule are `CryptoPkg/Library/OpensslLib/OpensslLib*.inf`
      (`DEFINE OPENSSL_PATH = openssl`), `CryptoPkg/Library/MbedTlsLib/MbedTlsLib*.inf`,
      `MdeModulePkg/Library/BrotliCustomDecompressLib` (`brotli/c/...`),
      `MdePkg/Library/BaseFdtLib` (`DEFINE FDT_LIB_PATH = libfdt/libfdt`),
      `MdePkg/Library/MipiSysTLib` (`DEFINE MIPI_SOURCE_PATH =
      mipisyst/library/src`), `SecurityPkg/DeviceSecurity/SpdmLib/Spdm*.inf`
      (`libspdm/library/...`), `MdePkg/Library/ArmTransferListLib`
      (`libtl/src/generic/...`), plus `RegularExpressionDxe` (oniguruma),
      `RedfishPkg/Library/JsonLib` (jansson) and `TcgTpmPkg`'s `TpmLib`
      (TPM), which neither platform builds; BaseTools' own Brotli is a
      host tool and in no image.
    - *Checked by another route.* The derivation reads the report's
      volumes and library lists and the INFs. The check shares only the
      volumes' module lists (the parser refuses an FV subsection without
      its volume name or entries, which BuildReport.py writes when it
      cannot read a volume's `.Fv.txt`), and reads neither the library
      lists nor the INFs: for each module it finds the module's build
      directory (or its FILE_GUID twin's, by `MODULE_FILE`), the libraries
      in its `static_library_files.lst`, and in their `OUTPUT` the first
      prerequisite of every object's gcc `.deps` file, the source
      compiled. A module with objects but no list, or an object without
      `.deps`, is a failure. So only compiled sources count on both
      routes: an INF's `[Sources]` headers do not make a part, and a
      feature flag on an entry is not evaluated (an entry it turns off
      would count, and the routes would then disagree, loudly). The two
      must name the same parts (they do, on all four images). The check also refuses an
      entry not `{path, chosen}`, a path that is not a file in
      `LICENSES/` or not among the top-level `licenses`, a list without
      edk2's licence, and a dual-licensed part without both its texts and
      exactly its permissive one chosen.
    - *The dual-licensed parts.* libfdt is BSD-2-Clause OR GPL-2.0+, in
      two files named so; libtl MIT OR GPL-2.0-or-later, in
      `LICENSE-MIT.md` and `LICENSE-GPL-2.0-or-later.md`, with a
      `LICENSE.md` stating the choice. The release relies on the
      permissive one, so it is `chosen: true` and the GPL text false;
      libtl's `LICENSE.md` is no choice and is true. Mbed TLS is
      Apache-2.0 OR GPL-2.0-or-later too, but in one `LICENSE` holding
      both texts: one entry, true. Mbed TLS is in no r2 image anyway.
    - *r2's lists.* X64 (both flavours): edk2 and OpenSSL (TlsDxe,
      IScsiDxe and Hash2DxeCrypto among the modules linking
      OpensslLib). AARCH64: edk2, OpenSSL, libfdt (ArmPlatformPkg's Sec
      and PlatformPei, ArmVirtPkg's MemoryInitPei link BaseFdtLib) and
      libtl (Sec and PlatformPei link ArmTransferListLib). Brotli, Mbed
      TLS, MIPI SyS-T and libspdm are compiled into no module of either
      image (no FV module links their libraries); their texts stay in
      the top-level `licenses`, which still lists every part whose
      sources or headers a module's makefile names (the include paths
      of MdePkg, MdeModulePkg and CryptoPkg name them).
    - *The report does not change the images.* `build` only records
      the options until the build ends, then writes the report from what
      was built (`BuildReport.py`; no AutoGen or GenFds code reads the
      report options). Measured on AlmaLinux 10 x86_64, gcc 14, X64 both
      flavours, clean, in the same workspace path (the path is in DEBUG's
      strings), four builds: two with `-y` (the r2 build and one more),
      two without. `vars.fd` and `Shell.efi` were identical in all four,
      and every module's `.efi` but one. The one is IScsiDxe, and it is
      not the report's doing: its AutoGen sizes the module-scoped
      `PcdEntryPointOverrideDefaultValue` override `"no"`
      (`OvmfPkg/Include/Dsc/NetworkComponents.dsc.inc`) as 3 bytes in
      some builds and as the DEC default `"yes"`'s 4 in others, a race in
      edk2's parallel AutoGen. RELEASE's IScsiDxe flipped between the two
      builds *without* `-y` (and the second matched the first r2
      build's `code.fd`), and between the two r2 builds, both with `-y`; DEBUG's came out one way with `-y` and the other
      without, twice each. The value is `"no"` either way, so the
      behaviour is the same. So X64 at this tag is reproducible except
      for that one module (§5.2); AARCH64 not at all.

23. **The FV walker cross-checked by VolInfo** (owner, 2026-11-25, for r2:
    two independent parsers must agree). Every check of what an image
    holds rests on one parser, uefi_firmware (§4.15). Before packing, each
    code image is read again by edk2's own VolInfo, from the build's
    workspace BaseTools (never a host copy), and the two sets of FFS file
    GUIDs must be equal; the release fails if they are not.
    - *How VolInfo is run at the pin.* It reads one volume per run
      (`--offset`; X64's `code.fd` has two top-level volumes,
      FVMAIN_COMPACT and SECFV, AARCH64's one, at 0x1000), found by their
      `_FVH` headers and skipped whole. It decodes a GUID-defined (LZMA)
      section only with the tool `GuidedSectionTools.txt` names, and at
      edk2-stable202611 it takes that file from beside the file it reads
      (`LoadGuidedSectionToolsTxt`; `-x` is a GUID cross-reference, not
      this). So the image is copied into a temporary directory with the
      `GuidedSectionTools.txt` its build wrote into `FV/`, and run with
      the workspace's `BinWrappers/PosixLike` first on PATH (for
      `LzmaCompress -d`). Without the file it says
      "EFI_SECTION_GUID_DEFINED cannot be parsed".
    - *Strict parsing.* VolInfo's output is for people. Every line must
      match one of the forms it writes for these images (header fields,
      file and section fields, depex opcodes, the a-priori lists, section
      start and end markers); the `File Name:` lines must add up to the
      "There are a total of N files in this/the child FV" totals; one
      "this FV" total per run; a non-zero exit or anything on stderr fails.
      The decompressor prints "Decoding" to the same stdout, unbuffered,
      so it lands inside whatever line VolInfo had buffered: it is cut out
      before parsing. VolInfo ignores its decompressor's exit status
      (`system()` in `VolInfo.c`) and writes its own errors to stdout with
      status 2, so a decode that failed in silence shows only as an empty
      encapsulated section, with totals that still add up: an empty
      encapsulation fails, and every LZMA GUID-defined section must have
      its "Decoding". edk2's wrappers run `$EDK_TOOLS_PATH/Source/C/bin/`
      when that is set, so it is set to the workspace's BaseTools, never
      the caller's. Pad files (all-F GUID) are dropped, as the walker
      drops them.
    - *Measured* on r2's four images (AlmaLinux 10 x86_64): X64 150 GUIDs,
      AARCH64 123, the same on both readings, both flavours.

24. **X64 code for baseline x86-64, and checked** (owner, 2026-11-25, for
    r2). edk2's GCC X64 flags name no `-march`, so the compiler's own
    default decides. GitHub's Ubuntu 26.04 runners install Ubuntu's
    `amd64v3` packages, whose gcc (15.2.0-16ubuntu1, `--with-arch-64=x86-64-v3`)
    defaults to x86-64-v3, and so does AlmaLinux 10's (RHEL's) gcc 14.4.1
    (`--with-arch_64=x86-64-v3`), so local builds were v3 too. r1's X64
    images, built on the runner, hold BMI1, BMI2,
    MOVBE and LZCNT instructions: they fault (#UD) on a CPU before Haswell,
    and under TCG with a CPU model that lacks those (`qemu64`, `Nehalem`).
    The boot checks missed it: X64 boots under KVM on the runner's own CPU,
    and the TCG fallback uses `-cpu max`.
    - *The pin.* `build.sh` appends `-march=x86-64` to each flavour's
      `<TARGET>_GCC_X64_CC_FLAGS` in the workspace's own `Conf/tools_def.txt`
      (`pin_x64_march`), the copy BuildEnv makes of the template, and
      refuses a template without exactly one such line per flavour built.
      No edk2 file is patched. `build` has no option that adds tool flags
      (its `-D` defines reach only what a DSC reads, and `OvmfPkgX64.dsc`'s
      `[BuildOptions]` read none for this), and a DSC `[BuildOptions]` or an
      overlay DSC would be a patch to edk2's source; the workspace's
      `tools_def.txt` is edk2's own place for a toolchain's flags, and
      every X64 C compile, of every module and library, starts from those
      lines (the DSC's and INFs' flags are appended to them, and none names
      a `-march`). Only the C compiles need it: NASM and `.S` sources are
      hand-written, and with LTO the link generates code from the options
      each object was compiled with (measured with Ubuntu's amd64v3 gcc
      15.2.0: an LTO object compiled with `-march=x86-64` and linked
      without it has no BMI, MOVBE, LZCNT or POPCNT instruction, and one
      compiled without it and linked with it still has them). BaseTools'
      host tools are built for the build host and are not part of an image.
    - *AARCH64 is not affected.* The cross compiler
      (`gcc-15-aarch64-linux-gnu` 15.2.0-16ubuntu1cross1) has no
      `--with-arch`/`--with-cpu`, and its predefined macros are the same on
      a runner-like `amd64v3` install and a plain one (`-march`/`-mcpu`
      unset, `__ARM_ARCH 8`, no `__ARM_FEATURE_*` beyond the default).
    - *The check* (`lib/axlfw/isa.py`, before packing, X64 DEBUG and
      RELEASE). Every module in the code FD's volumes, by edk2's build
      report (as the licence lists read it, §4.22), is checked two ways:
      its `GNUmakefile`, and each linked library's, compiles C with
      `-march=x86-64` as its last `-march`; and its ELF
      (`DEBUG/<name>.debug`, whose `.text` GenFw makes the module's PE),
      disassembled by the host's `objdump -d -M intel`, holds inside its
      function symbols (STT_FUNC with a size) no instruction of a later
      level, nor of an extension in no level. The levels are the x86-64
      psABI's: v2 CMPXCHG16B, LAHF-SAHF, POPCNT, SSE3, SSSE3, SSE4.1,
      SSE4.2; v3 AVX, AVX2, BMI1, BMI2, F16C, FMA, LZCNT, MOVBE, OSXSAVE; v4
      AVX-512. In no level, and so on no CPU by its level alone: AES-NI,
      PCLMULQDQ, SHA, ADX, GFNI, RDSEED. Each is scanned by the mnemonics it
      adds (`isa.EXTENSIONS`, held whole by a unit test); AVX, AVX2, FMA,
      F16C and AVX-512 as every VEX or EVEX mnemonic (a `v` mnemonic other
      than VMX, SVM, VMGEXIT, VERR and VERW; objdump's `{vex}` and `{evex}`
      pseudo-prefixes skipped to reach it), and AVX-512's mask instructions (`k...`);
      `pextrw` to memory is SSE4.1's (to a register, SSE2's). The flags
      route also refuses an extension enabled after the `-march`
      (`-mbmi2`, `-maes`, ...). A function symbol inside another is merged
      into the outer's range.
      OSXSAVE is the OS's XSAVE support, not an instruction a compiler
      emits. TZCNT stays in the list though a CPU without BMI1 runs its
      encoding as BSF: GCC emits that encoding at baseline only when
      optimizing for speed, and edk2 builds `-Os`. Only function symbols
      are read because edk2's linker script puts `.rodata` into `.text`:
      on a baseline build, the whole `.text` decodes 143 data bytes as
      instructions of later levels (137 VEX, 5 LAHF/SAHF, 1 SSE3; 78 in
      TlsDxe's tables), where the functions hold none. What lies outside
      them is hand-written NASM or GAS, which `-march` does not touch and
      which edk2 runs only after a CPUID check. ResetVector, NASM straight
      to a binary, has no ELF and no object, and is counted as without
      code. Not re-run by `build.sh check`, which has no workspace.
    - *Measured* (Ubuntu 26.04 with the runner's `amd64v3` gcc 15.2.0, in a
      container on AlmaLinux 10 x86_64; X64 RELEASE). Without the pin:
      2055 instructions beyond x86-64 in 118 of the 150 modules (BMI2
      1106, MOVBE 500, BMI1 440, LZCNT 9; 47 in SecMain, 5 in PeiCore,
      80 in DxeCore, 53 in the Shell), and 357 makefiles without the
      pin; TCG with `-cpu qemu64` and `-cpu Nehalem` stops at once on
      "X64 Exception Type - 06(#UD - Invalid Opcode)", `-cpu max` reaches
      the Shell. On AlmaLinux 10's own gcc 14.4.1, the r2 workspace built
      before the pin fails the same way: 357 makefiles, and 1887 (DEBUG)
      and 1972 (RELEASE) instructions in 117 modules. With the pin (X64
      DEBUG and RELEASE, the runner's gcc): no makefile without
      it and no instruction beyond x86-64, in all 150 modules (RELEASE
      14751 function ranges and 1054972 instructions, DEBUG 17571 and
      1343322)
      and their 207 libraries, and both images reach the Shell under TCG
      with `qemu64` and `Nehalem` (9 s).
25. **CI's QEMU main loop on epoll** (owner, 2026-11-25; §5 item 7's cause).
    Before the boots, CI sets `kernel.io_uring_disabled=2`, so QEMU's main
    loop uses epoll, as on the AlmaLinux 10 hosts the boots were developed
    on (whose kernel sets it so), and the log states the setting and
    QEMU's version. Never a retry. Ubuntu
    26.04's QEMU (`1:10.2.1+ds-1ubuntu3.3`) uses io_uring for its main loop
    (`fdmon-io_uring`) where the kernel allows it, and 10.2.1 can lose a
    wakeup there, which is what the record of the one hang since points to
    (§5 item 7; not reproduced with io_uring enabled, since the hosts here
    disable it and a shared host's kernel setting was left alone): fixed
    upstream by 961fcc0f22 ("fdmon-io_uring: check CQ
    ring directly in gsource_check", 2026-02-13) with its companion
    2ae361ef1d, both first in QEMU v10.2.2. A fixed QEMU in CI is §5
    item 8.

26. **Patch 0005, PciSioSerialDxe in ArmVirtQemu** (owner, 2026-11-25, for
    r3). An AARCH64 guest given a PCI 16550 UART (QEMU's `-device
    pci-serial,chardev=...`) gets a second EFI_SERIAL_IO_PROTOCOL beside
    the PL011's, as an X64 guest already does: a consumer can talk to a
    second serial line from UEFI on both arches. The patch adds
    MdeModulePkg's `PciSioSerialDxe.inf` to `ArmVirtQemu.dsc` (after
    PciHostBridgeDxe) and to `ArmVirtQemuFvMain.fdf.inc` (after the PCI
    drivers), regenerated, like 0001, from copies before and after it.
    `OvmfPkgX64` has always built the same INF, so the check's GUID
    (`E2775B47-D453-4EE3-ADA7-391A1B05AC17`) moves from the X64-only list
    to the common one (`checks.COMMON_DRIVERS`). The patch touches
    ArmVirtPkg and OvmfPkg's `PlatformBootManagerLibLight`, which only
    ArmVirtQemu (of the platforms built) links, so X64 is built as without it.
    - *The console is unchanged.* Of the serial devices,
      `PlatformBootManagerLibLight` (ArmVirtQemu's boot manager library)
      adds only the PL011 (SerialDxe's vendor path) and virtio serial ports
      to ConIn, ConOut and ErrOut; the PCI port is a console only when a
      console variable names it. OVMF's X64 `PlatformBootManagerLib` adds a
      PCI 16550 to the console itself (`IS_PCI_16550SERIAL`), as before.
    - *Connected by BDS, with or without a boot order* (owner, 2026-11-25,
      for r3: amended into 0005). ArmVirtQemu's BDS calls
      `EfiBootManagerConnectAll` only when QEMU gives no boot order
      (`ConnectDevicesFromQemu` fails); with a `bootindex` on any device it
      connects just the boot devices, so the first 0005 left the PCI port
      without EFI_SERIAL_IO until something connected it (the Shell's
      `connect -r`, or an application's `ConnectController`). The amendment
      adds an `IsPci16550` filter (PCI class 07h, subclass 00h, interface
      02h: `IS_PCI_16550_SERIAL`) to `PlatformBm.c` and connects each match
      non-recursively in `PlatformBootManagerBeforeConsole`, next to the
      virtio serial setup, as that function already connects displays,
      virtio input and RNG devices. Non-recursive: PciSioSerialDxe makes
      the UART child; TerminalDxe binds it only when a console variable
      names it, so the console is unchanged. X64 connects it in either
      case, as a console. *Measured* (AlmaLinux 10 x86_64, QEMU 10.0.0,
      AARCH64 DEBUG under TCG `cortex-a57`, `-chardev null` plus
      `-device pci-serial`, `tests/console-repro/repro.py`): with
      `bootindex=0` on the boot disk, `dh -p SerialIo -v` before any connect
      lists 2 handles, `VenHw(D3987D4B-971A-435F-8CAF-4967EB627241)/Uart(38400,8,N,1)`
      and `PciRoot(0x0)/Pci(0x2,0x0)/Uart(38400,8,N,1)`; without a boot
      order, the same 2. r2: 1 (the PL011's) both ways; the first 0005: 1
      with `bootindex` (above). Guarded by the patch check (§4.28), which
      requires `PlatformBm.c` compiled from the patched copy.
    - *Checked on every release* by the driver check (PROPERTIES.md), RED
      with the patch removed.
    - *Measured* (AlmaLinux 10 x86_64, QEMU 10.0.0; AARCH64 DEBUG under TCG
      `cortex-a57`, X64 DEBUG under KVM), each with `-chardev
      socket,id=pciser,path=...,server=on,wait=off -device
      pci-serial,chardev=pciser` added to the boot checks' command line.
      r3 AARCH64: `dh -p SerialIo` lists two handles,
      `VenHw(D3987D4B-971A-435F-8CAF-4967EB627241)/Uart(38400,8,N,1)` (the
      PL011, through SerialDxe) and `PciRoot(0x0)/Pci(0x2,0x0)/Uart(38400,8,N,1)`,
      and `sermode` both. The Shell ran on the PL011 as before; the socket
      got only TerminalDxe's 33 bytes of screen reset (`ESC[2J`,
      `ESC[001;001H`, `ESC[=3h`), no console text. With
      `PciRoot(0x0)/Pci(0x2,0x0)/Uart(38400,8,N,1)/VenVt100()` set in
      ConIn, ConOut and ErrOut (`setvar ... =--<path>`) and a `reset`, the
      console was on both ports: `echo` typed on the socket ran and was
      echoed on the socket and the PL011, and `echo` typed on the PL011
      reached the socket. Control, r2's AARCH64 DEBUG under the same command
      line: one SerialIo, the PL011's; r2's AARCH64 images fail the driver
      check (`lacks: PciSioSerialDxe (E2775B47-...)`). With
      `bootindex=0` on the boot disk, r3 lists one SerialIo until `connect
      -r`, then two. X64, r2 and r3 alike: two SerialIo, the PCI port's
      `PciRoot(0x0)/Pci(0x2,0x0)/Uart(115200,8,N,1)` and COM1's
      `PciRoot(0x0)/Pci(0x1F,0x0)/Serial(0x0)/Uart(115200,8,N,1)`, the PCI
      port a console from the start (an `echo` typed on it ran). X64's build
      reads nothing the patch changes: no file of its build directory, nor
      its build reports, names ArmVirtPkg (AARCH64's report names it 66
      times). A byte comparison with a build without patch 0005 (the
      sabotage build below) does not settle it, since that build ran in
      another workspace path, which DEBUG's strings and RELEASE's
      makefiles (a response file for the longer path, another object
      order) carry; RELEASE's PciSioSerialDxe.efi is identical in both.
      r3's text modes equal r2's on all four image sets.
    - *No boot check for the second SerialIo* (owner, 2026-11-25): the static
      driver check stays the release's permanent guard, as for patch 0001's
      drivers; the boot above is the one-time proof. A per-release
      `pci-serial` boot (one more boot per image set) was offered and declined.

27. **`hardware_images` in the manifest** (owner, 2026-11-25, for r3). A
    top-level map from each arch to the image directory a consumer ships
    to real hardware: the Shell and firmware deployed onto physical
    machines, where a DEBUG build's ASSERT (deadloop) can halt the box.
    r3's is `{"x64": "release/x64", "aarch64": "release/aarch64"}`
    (`release.HARDWARE_IMAGES`). `default_flavour` stays `DEBUG`: it says
    what QEMU guests boot, and the descriptors' numbering still picks DEBUG.
    Additive only: format stays 2 and no stable field changes
    (RELEASE-FORMAT.md). SoftBMC will read it.
    - *Checked* (`release.check_hardware_images`) before any boot, with the
      static checks, and again by `build.sh check` on the packed manifest:
      one entry per arch, exactly `x64` and `aarch64`; each naming one of
      the manifest's images, of the entry's own arch, whose flavour is
      `RELEASE`. Any other entry fails the release.

28. **r3 console: patches 0006-0010, and the patch check** (owner,
    2026-11-25, for r3: inventory items 2, 3, 11, 16 and 19). Each patch is
    regenerated, like 0001, from copies before and after it, and applies
    after 0005. Measured on AlmaLinux 10 x86_64 with QEMU 10.0.0, X64 DEBUG
    under KVM and AARCH64 DEBUG under TCG `cortex-a57`, by
    `tests/console-repro/repro.py` (a guest application of its own,
    `AxlConsoleReproPkg`'s `Probe.efi`, built with the release build's
    BaseTools, reads what the Shell cannot), on r2's installed images (RED)
    and on this build (GREEN):
    - *0006, the PS/2 mouse on q35* (OvmfPkg `SioBusDxe/SioService.c`).
      SioBusDxe's table had COM1, COM2 and the keyboard (PNP0303, UID 0);
      Ps2MouseDxe binds only PNP0F03, PNP0F13, or PNP0303 UID 1, so it never
      bound. The patch appends PNP0F13, UID 0, on the keyboard's ports
      (0x60, 0x64); the existing children keep their indices and paths. X64
      only (ArmVirtQemu has no SioBusDxe nor PS/2). Measured, no USB or
      virtio mouse, three HMP `mouse_move 10 5`: r2 has no SimplePointer
      with a device path; the console branch's r3 (PNP0F03, as the
      inventory named it) had
      `PciRoot(0x0)/Pci(0x1F,0x0)/Acpi(PNP0F03,0x0)` (ResolutionZ 0), whose
      states sum to x 30, y 15. *PNP0F13 from integration* (owner,
      2026-11-26, §5 item 9): QEMU's DSDT names the mouse PNP0F13
      (`hw/input/pckbd.c`, `aml_eisaid("PNP0F13")`), so 0006 now does too;
      the reproducer now requires `Acpi(PNP0F13,0x0)`, RED on the console
      branch's build (`Acpi(PNP0F03,0x0)`, sums 30, 15, 0: NOT FIXED) and
      GREEN on the integrated build (§4.40's table).
      OVMF's BDS connects it only without a QEMU boot order, and QEMU sends
      it events only while it is the active mouse (CONSUMERS.md).
    - *0007, serial Ctrl-S* (ShellPkg `ShellProtocol.c`, `ConsoleLogger.c`;
      §5 item 6). `NotificationFunction` sets `HaltOutput` for 0x13 (19) as
      well as Control+'s', as its break takes 3 beside Control+'c'; the
      pause loop in `ConsoleLoggerOutputString` pauses again on 0x13 too,
      which matters: the 0x13 is also queued as a key, and the loop reads it
      first, so without the second change it would resume at once. The next
      key then resumes. *As a keyboard's Ctrl-S, also in editors* (from the
      code, not measured; found in review): `edit` and `hexedit` save on a
      bare 0x13 (`MenuBarDispatchControlHotKey`), and their next output then
      pauses until a key, which the pause loop takes, as a USB keyboard's
      Ctrl-S already did; and a terminal sending XOFF (0x13) for software
      flow control now pauses the Shell (§5 item 10). Measured with the `ctrl-s` check's own function
      (below): r2, 29 ticks (X64; tick 14 to 42) and 30 (AARCH64) in the 3 s
      from 1 s after the 0x13; r3, none, then tick 4 after a space, both
      arches.
    - *0008, TerminalDxe's wide characters* (MdeModulePkg
      `TerminalConOut.c`). For every terminal type but VT-UTF8,
      `TerminalConOutOutputString` cast a non-box-drawing CHAR16 to CHAR8
      and tested the byte: U+0141 went out as 'A', U+010D as CR. It now
      tests the CHAR16 (`TerminalIsValidAscii` and
      `TerminalIsValidEfiCntlChar` take one) and compares it, not the byte,
      with ESC. Measured with each serial terminal switched to VT100
      (`ConnectController` with a VenVt100 node, which TerminalDxe honours),
      writing `wide<` U+0141 U+0158 `>`: r2 `wide<AX>`, r3 `wide<??>`, both
      arches (X64's default is VT-UTF8, which never took that path; AARCH64's
      was VT100 until §4.29).
    - *0009, DeleteFileByName of an absent file* (ShellPkg
      `ShellProtocol.c`). It opened through `EfiShellCreateFile`, with
      `EFI_FILE_MODE_CREATE`: an absent file was created, deleted, and
      `EFI_SUCCESS` returned. It now opens read/write without create
      (`InternalOpenFileDevicePath`), so `EFI_NOT_FOUND`; a `>v` name (an
      environment variable) still goes through `EfiShellCreateFile`.
      Measured on a virtiofs share (virtiofsd 1.13.3) watched by inotify on
      the host: r2 `Success (0x0)`, inotify `create absent.txt`,
      `delete absent.txt`; r3 `Not Found (0x800000000000000E)`, no event;
      a missing directory on the path (`newdir\absent.txt`) gave Not Found
      and no event on both. The Shell's own callers (cp, mv, redirection)
      ignore the status.
    - *0010, the virtio mouse's wheel* (OvmfPkg `VirtioInputDxe`).
      `REL_WHEEL` (0x08) adds into `RelativeMovementZ`, cleared with X and Y
      by `GetState`; `ResolutionZ` is 8 (the driver's X and Y resolution, as
      UsbMouseDxe's) when the device's EV_REL bitmap has REL_WHEEL, else 0.
      Measured with `-device virtio-mouse-pci` set active (`mouse_set`),
      HMP `mouse_move 0 0 1` twice, `0 0 -1`, `4 0`: r2 ResolutionZ 0, sums
      x,y,z 4, 0, 0; r3 ResolutionZ 8, sums 4, 0, 1, both arches.
    - *The `ctrl-s` boot check* (`boot.ctrl_s_pause`, every image set, a
      serial boot of its own after `ctrl-c`): the ticks script; 0x13 after
      `tick 3`; 1 s allowed for ticks already sent (`CTRL_S_SETTLE`); then
      no tick for 3 s (`CTRL_S_QUIET`); a space must bring the next tick
      within 10 s (`CTRL_S_BOUND`); then 0x03 must bring the prompt within
      `CTRL_C_BOUND`, and `echo` must run. The same boot first types `echo
      abX`, DEL, `c`, which must print `abc` (§4.29). Unit `CtrlSTests`
      (stand-in Shells that ignore 0x13, never resume, end first, and read
      DEL as Delete), each check mutated away in turn, RED. Control: r2
      (above). Sabotage: the build without 0007 (PROPERTIES.md).
    - *The patch check* (`checks.check_patches`, with the static checks,
      before any boot). Every name in `checks.REQUIRED_PATCHES` (every file in
      `patches/`, a unit test holds the two equal) must be among the patches the build
      applied; each file the patches touch must be, in the workspace's
      patched copy (`ws/overlay`), the source's with the patches applied;
      and each patched C, header or
      assembly file must be named, as the copy, by at least one object's gcc
      `.deps` in the builds (GUARDED's two too), and as the source's by none. So a patch
      dropped, one that did not land, or a build that read the source's
      file (a `PACKAGES_PATH` out of order) fails. `build.sh check`, which
      has no workspace, requires every required name in the manifest's
      `edk2.patches`. Unit `PatchCheckTests` and a `ReleaseCheckTests`
      case, each branch mutated away in turn, RED. On the real workspace:
      the overlay's `SioService.c` replaced by the source's, VirtioMouse's
      `.deps` pointed at the source, and 0007 left out of the list, each
      named exactly; it also caught a workspace built from an earlier text
      of 0009 (two comment lines the patch had since changed). About 2 s.
      *The overlay compared whole* (review, 2026-11-25): the first version
      looked for each added line anywhere in the patched file, which a later
      patch changing an earlier one's line would fail, and which generic
      lines (`  }`) would pass unapplied; the check now replays the patches
      in order onto fresh copies of the source's files (`checks.replay`, `git
      apply`, as `build.sh`) and compares each with the overlay's, byte for
      byte.
29. **AARCH64's serial console is VT-UTF8** (owner, 2026-11-25, for r3:
    inventory item 12). `ArmVirtQemu.dsc` sets `PcdDefaultTerminalType|1`
    (VT100), and `PlatformBootManagerLibLight` puts
    `PcdTerminalTypeGuidBuffer` (OvmfPkg's default, VT100) in the console
    paths; X64's `PlatformBootManagerLib` names VT-UTF8 (`gVtUtf8Terminal`).
    `build.sh` now passes, for AARCH64 only, as it passes the front-page
    PCDs: `PcdDefaultTerminalType=3` and
    `PcdTerminalTypeGuidBuffer=H{...}` (`EFI_VT_UTF8_GUID`,
    AD15A0D6-8BEC-4ACF-A073-D01DE77E2D88). No patch. X64's
    `PcdDefaultTerminalType` stays 0, as in r2 (TerminalDxe's AutoGen: X64
    0, AARCH64 3 and the VT-UTF8 bytes in BdsDxe's, both flavours).
    - *What changes.* VT-UTF8 reads DEL (0x7f) as Backspace
      (`TerminalConIn.c`, upstream 58c9ba24); VT100 as Delete. Box drawing
      and characters beyond ASCII go out as UTF-8. Virtio serial consoles,
      which take the same GUID, and a PCI 16550 connected without a type
      (`PcdDefaultTerminalType`) are VT-UTF8 too.
    - *Measured* (as §4.28): `echo abX`, DEL, `c` printed `abXc` on r2's
      AARCH64, `abc` on r3's; X64 `abc` on both. The text modes the build
      measured (`text_modes`, all four displays, both flavours) equal r2's
      on every image set: AARCH64 serial still `[80,25] [80,50] [100,31]
      [128,40] [160,42] [240,56]`, current 240x56.
    - *A vars file from r2.* An AARCH64 vars file that r2 had booted holds
      ConOut with the VenVt100 path; r3's BDS appends the VenUtf8 one
      (ConOut 0x3F bytes, then 0x7E), and TerminalDxe takes the type of the
      last path connected: DEL was Backspace. The VT100 entry stays in the
      variable.
    - *Checked* by the `ctrl-s` boot's DEL (§4.28) on all four image sets:
      RED on r2's AARCH64 images (`abXc`).
30. **Patch 0011, VirtioFsDxe serialized at TPL_CALLBACK** (owner,
    2026-11-25, for r3; inventory item 1). No VirtioFsDxe entry point
    raised the TPL, and every request shares one virtqueue:
    `VirtioPrepare()` starts each chain at descriptor 0 and `VirtioFlush()`
    waits for the used index to equal the one it published. A
    TPL_CALLBACK caller (a timer, a resident driver's tick) doing file I/O
    while a lower-TPL call is mid-request overwrites that request's
    descriptors or carries the used index past it, and the lower call
    polls forever (axl-sdk's console-device-hold hang, its
    AXL-Test-Guest-Pool-Design.md "The share cannot take a read from a
    timer callback").
    - *EFI_LOCK, not a bare RaiseTPL*, as EnhancedFatDxe does (FatFsLock):
      one driver-global `mVirtioFsLock` at TPL_CALLBACK, taken by a wrapper
      around each of the eleven member functions a protocol instance
      carries (`SimpleFsLock.c`); the functions behind the wrappers call
      one another unlocked (Open() calls OpenVolume()). The lock's ASSERT
      on a second acquire would expose any re-entry the TPL raise allows
      (none was found). Stop() follows FAT too: it checks for open files
      under the lock, uninstalls the protocol without it (an uninstall may
      run other drivers' Stop or Start, which may call in), and checks again
      under the lock before freeing the device; if a stale holder opened a
      file in between, it reinstalls the protocol and connects again the
      drivers the uninstall disconnected, as the core does when an
      uninstall fails (both from the reviews).
    - *What changes for callers*: a file call above TPL_CALLBACK (outside
      the UEFI spec) now ASSERTs in DEBUG, as on FAT; a long Read or Write
      holds TPL_CALLBACK work (timers, network DPCs) off for its duration,
      as on FAT.
    - *Measured* (AlmaLinux 10 x86_64, QEMU 10.0.0, virtiofsd 1.13.3; X64
      under KVM, AARCH64 under TCG `cortex-a57`) with
      `tests/repro/repro.py vfs`: VfsRace.efi, run from the share, reads a
      3000-byte file whole (open volume, open, read, GetInfo, close both)
      in a loop, while a 1 ms periodic timer at TPL_CALLBACK does the same
      (OVMF's 10 ms tick makes it 100 rounds a second), until the timer
      has done 1000 rounds; every read must equal the first. r2, DEBUG:
      X64 hung within the first 2000 foreground rounds in each of the two
      runs (no progress line after `racing 1000 timer rounds`; frame chain
      VfsRace -> VirtioFsDxe (3 frames) -> DxeCore -> Metronome, the
      `Stall` in VirtioFlush's poll); AARCH64 hung the same way; r2
      RELEASE hung as well, X64 and AARCH64. r3, all four image sets: `vfsrace: PASS`,
      cb=1000, in 10.1-10.2 s (fg 5615-36839 rounds), no ASSERT in the
      DEBUG logs. Stop() and Start(): a `reconnect -r` after the race
      (X64 and AARCH64 DEBUG) stopped and restarted the share (X64's log
      shows SimpleFileSystem installed 4 times, not 2) and it listed
      race.txt again, without an ASSERT.

31. **Patch 0012, one APIC base MSR read per LocalApic register access**
    (owner, 2026-11-25, for r3; inventory item 15). Shape as axl-sdk
    drafted it (its AXL-Test-Guest-Pool-Design.md, "drafted, not sent";
    no diff existed): `ReadLocalApicReg`/`WriteLocalApicReg` take the mode
    and the xAPIC base from one `LocalApicReadMsrReg64 (MSR_IA32_APIC_BASE)`
    (`GetApicModeAndXApicBase`), so `SendApicEoi` costs 3 CPUID and 1
    RDMSR, not 6 and 2. The read still goes through
    `LocalApicReadMsrReg64`, so under TDX it is still a TDVMCALL; nothing
    is cached (the library is linked into SEC, PEI and SMM too), and
    `GetApicMode`/`GetLocalApicBaseAddress` are unchanged. AARCH64 does not
    build the library.
    - *Measured* (AlmaLinux 10 x86_64, Intel host, QEMU 10.0.0, KVM `-cpu
      host`, the boot checks' command line, an idle Shell, 20 s after 10 s
      to settle; HMP `info stats vcpu`, vCPU 0; vCPU 1 had no exits):
      the r3 X64 RELEASE build without 0012, 1361, 1331 and 1338 exits a second;
      with it, 912, 911, 909 and 912 (r2 RELEASE 1340, r3 930 on an earlier
      build). halt_exits 100 and io_exits 300 a second in every run: the
      100 Hz tick and the serial poll. About 420-450 fewer a second, 4.2-4.5
      a tick: on this host TdIsEnabled() is 2 CPUIDs, so each of the two
      MSR reads cost 4 exits. perf's kvm tracepoints (axl-sdk's method)
      need privileges this host does not grant.

32. **Patch 0013, an accepted TCP socket opens its IP child BY_CHILD**
    (owner, 2026-11-25, for r3, with a reproducer; inventory item 7).
    `TcpCloneTcb` shares the listener's IpInfo (`NET_GET_REF`) but never
    opened that IP instance BY_CHILD_CONTROLLER for the clone's handle, as
    `TcpAttachPcb` does for a socket's own. When the IP instance is
    stopped, the core gives TcpDxe's Stop() only its BY_CHILD openers (the
    listener); the clone's reference keeps the instance open BY_DRIVER,
    and `CoreDisconnectControllersUsingProtocolInterface` calls Stop()
    forever at TPL_CALLBACK. The patch opens it (closed by
    `SockDestroyChild`, which already closed `Tcb->IpInfo->ChildHandle`
    for every socket: a failing close for clones until now), and on that
    open's failure destroys the clone as unconfigured (it is on no TCB
    list). From the review: the `SockClone` failure path (a listener at
    its backlog) leaked the reference taken before it, which brings the
    same hang back after the listener goes; it now returns it.
    - *Teardown*: Stop() destroys the listener first (its flush destroys
      clones not yet accepted), then the accepted socket; its pending
      tokens complete EFI_ABORTED via `SockConnFlush`, as every other
      socket's do in Stop(). The peer is not told (no RST: `TcpDetachPcb`
      sends nothing; unchanged).
    - *Measured* (as §4.30; AARCH64 under TCG `-cpu max`, see §5 item 12)
      with `tests/repro/repro.py tcp`: TcpHold.efi, a resident UEFI
      driver, listens on 5555 (static 10.0.2.15), a host client connects
      through `hostfwd` and sends 6 bytes, which TcpHold receives, leaving
      a Receive pending; `dh -p <TCP4>` lists its two sockets (IPv4
      0.0.0.0 and 10.0.2.2); then `reconnect -r`. r2 DEBUG, X64 and
      AARCH64, and r3 without 0013 (X64 DEBUG): `reconnect -r` never
      returned (60 s); 40 PC samples all in DxeCore (CoreOpenProtocol,
      CoreDisconnectController, the handle database), no ASSERT; r2 X64
      and AARCH64 RELEASE the same. Control, r2: the same boot with no
      server returns in 0.6-1.1 s. r3, all four image sets: returns in
      0.4-0.7 s, both sockets gone, a second TcpHold loaded after it
      accepts and receives; no ASSERT in either DEBUG log.
    - *No ASSERT in DEBUG, and no teardown-order problem seen*; the
      AARCH64 `cortex-a57` ASSERT below is not TcpDxe's.
    - *Left upstream* (found by the review, not fixed): when a clone's
      `CreateCallback` fails, `SockCreate`'s error path
      (SockImpl.c:769-778) frees a socket already linked into its
      parent's ConnectionList, with ConnCnt counted. Unreached in the
      boots above.

33. **The reproducers are kept, not run per release** (builder,
    2026-11-25; the owner decides, §5 item 11). `tests/repro/`: ReproPkg
    (VfsRace, TcpHold), `build-apps.sh` (built with the last build's
    BaseTools, X64 for baseline x86-64) and `repro.py` (on
    `lib/axlfw/boot.py`'s QEMU command line). As with patch 0005, the
    one-time boots above are the proof; a patch that no longer applies
    fails the build.

34. **The guarded image, GUARDED** (owner, 2026-11-25, for r3; built by
    the release builder). A third image per arch, `guarded/x64` and
    `guarded/aarch64`: the DEBUG build with edk2's UEFI heap guard, so a
    write past a pool buffer or page allocation faults at the write instead
    of corrupting memory silently. SoftBMC asked for it: a DXE pool
    corruption passes every QEMU run and halts only on real hardware.
    - *The PCDs.* `PcdHeapGuardPropertyMask=0x03` (the UEFI page guard,
      BIT0, and pool guard, BIT1; BIT7 clear: a pool buffer sits against its
      tail guard page, which is what catches an overrun),
      `PcdHeapGuardPoolType=0x1E` and `PcdHeapGuardPageType=0x1E` (EfiLoaderCode,
      EfiLoaderData, EfiBootServicesCode, EfiBootServicesData). The pool and
      page guards, not the freed-memory guard (BIT4): edk2 cannot enable
      both (MdeModulePkg.dec, note c), and the reported failure is an
      overrun; a use-after-free image would be a fourth flavour. Not BIT6
      (non-stop), which logs and goes on. Not the runtime types:
      AARCH64's runtime granularity is 64 KiB, and `CoreAllocatePoolI`
      drops the guard for any granularity but a page, so they would be
      guarded on one arch only; on X64 they would also scatter guard holes
      through the runtime map an OS inherits. The types are what UEFI
      drivers and applications allocate by default
      (UefiMemoryAllocationLib's EfiBootServicesData) and what the Shell
      loads images as.
    - *All FixedAtBuild* (MdeModulePkg.dec:1143, 1179, 1207), so it is a
      build of its own: `build.sh` builds it in a workspace of its own,
      `work/ws/guarded`, with a copy of the pinned Conf, since a DSC's
      `OUTPUT_DIRECTORY` cannot be set from the command line and DEBUG's
      `Build/` must stay DEBUG's. The PCDs change three modules (DxeCore,
      DxeIpl, and on X64 CpuDxe); every other module is DEBUG's.
    - *No descriptor, so never auto-picked.* Both consumers' searches take
      the lowest-named qualifying descriptor (axl-sdk's
      `axl-firmware-select.py`, axlvmpool's `descriptors::candidates` and
      `discover::choose`), so a `30-` descriptor would never win while
      DEBUG's qualifies. But each falls through: axlvmpool takes the next
      candidate when one fails its driver check or is known bad (its
      catalog), axl-sdk's `--fallback` the first after a failed read, and a
      third reader (libvirt, a later consumer) may rank otherwise. With no
      descriptor the guarded image is never a candidate, whatever happens
      to DEBUG's and RELEASE's; and install.py's descriptor clean-up
      (`[12]0-axl-firmware-*`) is unchanged. A consumer selects it by path:
      the manifest's `images["guarded/<arch>"]`, `OVMF_CODE`/`OVMF_VARS`
      (`AAVMF_*`), or axlvmpool's `config.toml` `firmware.X64 = {code,
      vars}` (CONSUMERS.md). The manifest says `"descriptor": null`.
    - *Additive.* The manifest stays format 2: two more `images` entries,
      `flavour` `GUARDED`, `debug.heap_guard`, `measured.pool_overrun`;
      `hardware_images` still names RELEASE only (checked). install.py
      from r3 requires the four sets and checks every other set the
      manifest lists (`<name>/<arch>`), so a later image set needs no
      installer change; **r2's install.py refuses r3** ("its manifest's
      images are [...], not [...]"): an old checkout must be updated to
      install r3. No other installer exists yet (axlvmpool's is slice C1,
      axl-sdk's planned).
    - *No symbols tarball:* DEBUG's serve every module but the three above.
    - *Sabotage:* the guarded build without its PCDs is the DEBUG build
      (the same command line less the three), so each check was run on
      DEBUG's images and build as GUARDED: the static check says
      `<DxeMain>: the guarded image's PcdHeapGuardPropertyMask 0x00, not
      0x03; PcdHeapGuardPoolType 0x00, not 0x1e; PcdHeapGuardPageType
      0x00, not 0x1e` on both arches, and the boot check `Overrun.efi did
      not fault on a write one byte past a pool buffer; fault was
      expected` on both; and GUARDED's build checked as DEBUG, `a DEBUG
      build with the heap guard (PcdHeapGuardPropertyMask 0x03)`. With the
      PCDs, `measured.pool_overrun` is `fault` on both GUARDED sets and
      `silent` on both DEBUG sets (local build, AlmaLinux 10 x86_64).
    - *Checked.* Statically, before any boot (`checks.heap_guard`, from
      DxeMain's AutoGen): GUARDED's three PCDs exactly; every other image's
      `PcdHeapGuardPropertyMask` 0. Booted, by `guard.check` on each DEBUG
      and GUARDED set: `tests/guard-overrun/`'s `Overrun.efi` (built by
      `build.sh` out of tree, against the pinned MdePkg) allocates 64 bytes
      of EfiBootServicesData and writes byte 64; GUARDED must fault (X64:
      `#PF`, `W:1 P:0`, the image at RIP `Overrun.dll`; AARCH64: a
      synchronous exception whose first frame is `Overrun.dll`), DEBUG must
      print its second line and give the prompt back. The DEBUG run is the
      control: the write is made, and only the guard catches it. Not re-run
      by `build.sh check`, which has no workspace and so no application.
    - *Measured* against DEBUG side by side (AlmaLinux 10 x86_64, QEMU 10.0.0;
      X64 under KVM `-cpu host`, AARCH64 under TCG `cortex-a57`, `-m 1024`,
      `-smp 2`; five boots each, DEBUG and GUARDED interleaved; **under
      load**, the host's 1-minute load 9.2 to 11.1 throughout from another
      project's tests, so the times are relative). Time from QEMU's start
      to the Shell's prompt, median (range): X64 DEBUG 4.50 s (3.76-4.64),
      GUARDED 4.99 s (4.21-6.06), +0.5 s; AARCH64 DEBUG 3.87 s (3.74-4.13),
      GUARDED 5.01 s (4.76-5.24), +1.1 s. Memory, the Shell's `memmap`
      at its prompt: X64 Available 248656 pages DEBUG, 237403 GUARDED
      (11253 pages, 44 MiB, more in use: guard pages and the page each
      guarded pool allocation takes); AARCH64 252109 and 245195 (6914
      pages, 27 MiB). Both boot to the Shell at `-m 256` and `-m 128` (one
      boot each): at 128 MiB X64 GUARDED has 8029 pages (31 MiB) free,
      DEBUG 19280; AARCH64 15823 and 22734. A consumer whose guest is
      tight on memory gives the guarded one more. In the release's own
      boot checks (2 at a time, same host and load) a GUARDED set's boots
      took 55 s, a DEBUG set's 38 s.
    - *CI's cost.* Locally (same host, load about 10, `JOBS=4`) the two
      guarded builds took 2:30 (X64) and 2:34 (AARCH64), as long as DEBUG's
      (2:38, 2:15), and the two guard-check applications seconds; the whole
      `build.sh build` 26:46. On CI's runner each edk2 build takes 2:15 to
      2:27 (run 45757564955), so the guarded builds add about 5 minutes;
      the boots about 1 minute more (two more image sets and four
      guard-check boots, two at a time), the `build.sh check` step's
      re-boots about 35 s, and packing (xz -9) about 30 s. About 7 minutes
      on run 45757564955's 15:40 build job, an estimate until the first CI
      run of r3 measures it. The release tarball grows from 8.1 MB (r2) to
      12.5 MB.

35. **The installer conformance fixture** (owner, 2026-11-25, axlvmpool
    DESIGN §11 item 105, D7; built by the release builder).
    `tests/conformance/`: `make-fixture.py` (standard library, Python 3.9,
    deterministic: members dated 2000-01-01, 0:0, 0644/0755, sorted; xz
    preset 6, CRC64; `--check` regenerates in memory and names any file
    that differs) writes `cases/<name>/` (a tarball and its SHA256SUMS) and
    `expected.json` (format 1). The miniature release, `edk2-stable200001-r1`,
    has r3's layout and a manifest with every field, stand-in images of a
    few dozen bytes, and the four real descriptors (`install.descriptor`).
    Cases: three good (six sets; pinned, no SHA256SUMS; r2's four sets),
    one locked (must wait), one with hostile modes and owners (installed,
    normalised), and 39 hostile, each with an `outcome`, a
    `stage` (digest, members, manifest, descriptors), a stable `reason`
    and install.py's exact message: the tarball's digest (SHA256SUMS
    wrong, pin wrong, SHA256SUMS without the asset, none, a bad asset
    name), the archive (truncated xz, an uncompressed tar), members (`..`,
    `..` under the top, absolute, outside the top, another release's top,
    symlinks and hard links inside and escaping, character and block
    devices, a FIFO, a PAX `path` climbing out, a duplicate, a member over
    128 MiB, members over 1 GiB in all, more than 4096 members), the
    manifest (format 1 and 3, the release, a missing set, an escaping set
    name, one with a newline, a reserved one, a moved image, a wrong
    digest, a guarded image's size, a missing file) and the descriptors
    (outside, absolute, retargeted, unlisted). `tests/test_conformance.py`
    installs each into a prefix holding an earlier install, another
    release's descriptor and a file of the user's: installed (files by
    sha256, descriptors exactly), refused (InstallError, the message, the
    prefix byte for byte as it was) or waits (with `.lock` held, nothing
    changes; then installed).
    - *install.py failed five cases, real bugs, fixed:* (1) a truncated xz
      raised `EOFError`, which neither install.py nor `__main__` catches:
      a traceback; (2) an uncompressed tar raised `tarfile.ReadError`,
      reported without the file; both are now "`<tarball>`: not a complete
      xz-compressed tar: ..."; (3) a duplicate member was extracted twice,
      the last copy winning, and refused only when that copy failed its
      digest (a duplicate of the manifest's bytes passed; one whose first
      copy differed was installed from the second): now refused by name;
      (4) a member of any size was written (a decompression bomb fills the
      disk): now refused over `MAX_MEMBER` (128 MiB, twice AARCH64's 64 MiB
      bank); (5) so was any total: now refused over `MAX_TOTAL` (1 GiB, r3
      unpacks to about 420 MiB). Members are checked as each header is
      read, before anything is written, so a bomb is refused without
      decompressing it.
    - *Found by the review, fixed* (each with a fixture case, RED before
      and with its check mutated away): the tarball's modes and owners
      were kept where Python lacks the "data" filter (before 3.12 and the
      backports), so a setuid or world-writable `code.fd` could be
      installed (`hostile-modes`: every member now 0644 or 0755, the
      user's); no cap on the member count (`too-many-members`, 4096); an
      image set name with a trailing newline passed the shape check
      (`set-newline`, `fullmatch`); and a set named `symbols/<arch>`,
      which `--symbols` writes (`set-reserved`; `descriptors`, `symbols`,
      `patches` and `LICENSES` are reserved).
    - *Each check shown to matter:* with it mutated away the fixture goes
      RED on its cases (member type: the links, devices and FIFO; `..`: the
      two climbs inside the top; duplicates, each cap, the archive errors,
      the descriptor path check: their cases). `_inside` alone goes RED on
      none: every path it guards is first held to its exact expected
      string, and links are refused as members, so it is defence in depth.
    - *Vendoring:* copy the directory; assert `outcome`, and `reason` (an
      installer that words its own messages) or `message` (`{tarball}`,
      `{tarball_dir}` and `{unpacked}` placeholders, as
      `test_conformance.pattern` fills them).

36. **Checks and docs for input and RNG** (owner, 2026-11-25, for r3).
    VirtioInputDxe (`F141B1E5-9C7C-44CC-AFAA-E87D7689B113`) and RamDiskDxe
    (`28A03FF4-12B3-4305-A417-BB1A4F94081E`) join `checks.COMMON_DRIVERS`:
    both are in every r1 and r2 image, X64 included (OvmfPkgX64.fdf:217 and
    :240; ArmVirtQemuFvMain.fdf.inc:121 and :228), so RamDiskDxe is common,
    not AARCH64's. Control: AlmaLinux 10's AAVMF (`QEMU_EFI-pflash.raw`)
    lacks VirtioInputDxe (`tests/firmware-tests.sh`). CONSUMERS.md: an
    absolute pointer is `-device virtio-tablet-pci` (measured on r2, both
    arches: `dh -p AbsolutePointer` lists `PciRoot(0x0)/Pci(0x2,0x0)`;
    `usb-tablet` gives none); networking needs an RNG on both arches
    (measured on r2 under TCG: no `eth0` under `-cpu qemu64` or
    `cortex-a57` alone; `eth0` with `+rdrand`, `-cpu max`, KVM `-cpu
    host`, or `-device virtio-rng-pci`). No document here said the
    absolute pointer cannot be used in QEMU; patch 0001's message ("a USB
    mouse or tablet") implies usb-tablet works, which it does not, and is
    left as it is (§4.7; the release builder touches no patch).

37. **Patch 0014, the TPM mapped in PEI on AARCH64, and the TPM boot check**
    (owner, 2026-11-25, for r3; found by the r3 release builder). An AARCH64
    guest with a TPM (`-chardev socket,...` to swtpm, `-tpmdev emulator`,
    `-device tpm-tis-device`) never boots: r2's DEBUG image prints its banner
    and "Synchronous Exception at 0x4A700", and stops there.
    - *The cause, the one patch 0003 fixed for the second PL011* (§4.19).
      Measured on r2's AARCH64 DEBUG under TCG `cortex-a57` (AlmaLinux 10
      x86_64, QEMU 10.0.0, swtpm 0.9.0), with QEMU's `-d int`: a data abort
      from EL1, ESR 0x96000006 (a level 2 translation fault), FAR 0xc000000,
      ELR 0x4a700; X20 0x0c000000. 0xc000000 is QEMU's TPM on `virt`'s
      platform bus: the device tree's `/platform-bus@c000000/tpm_tis@0`,
      `compatible = "tcg,tpm-tis-mmio"`, `reg = <0x0 0x5000>`, the bus's
      `ranges = <0x0 0x0 0x0c000000 0x02000000>`. On the r3 candidate's
      layout the fault is at 0x49748, which the FV map and
      `Tcg2ConfigPei.debug` place in Tcg2ConfigPei's `MmioRead8Internal`:
      its first read of the TPM (TPM_ACCESS, at PcdTpmBaseAddress, which
      PlatformPeiLib set from the same node). Since upstream `158c1b614`
      PEI's table (QemuVirtMemInfoLib) holds DRAM, the FV, the console UART
      and fw_cfg only; 202511's mapped `virt`'s whole peripheral window,
      0x08000000 for 128 MB, which holds 0x0c000000. No upstream fix or
      issue as of 2026-11-25.
    - *The patch* maps the TPM's MMIO region as the console UART's is
      mapped (device memory, CCA bit), after fw_cfg: the first
      `tcg,tpm-tis-mmio` node, its `reg` (one cell each or two each)
      translated through its parent's `ranges` exactly as PlatformPeiLib
      translates it for PcdTpmBaseAddress, so the page mapped is the page
      read; the whole `reg` (0x5000, five localities) rounded to pages.
      With no TPM node, or one that does not parse, nothing is added and
      the table is as before. The table grows by one entry, to 8, and the
      TPM is mapped only while an entry stays for the end; patch 0003's
      loop is unchanged. Applied after 0003 (the same file).
    - *Verified* (AlmaLinux 10 x86_64, QEMU 10.0.0, swtpm 0.9.0; AARCH64
      under TCG `cortex-a57`, X64 under KVM). RED: r2's AARCH64 DEBUG with
      the TPM, as above, no Shell within 60 s. GREEN, all four of this
      build's image sets with the TPM: the Shell, and `PcrRead.efi` read
      PCRs 0-7 (below). Without the TPM every r2 boot check passes
      unchanged, and the text modes on all four sets equal r2's manifest's.
      X64 is built as in r2 (the patch touches only ArmVirtPkg).
    - *The TPM boot check* (`lib/axlfw/tpm.py`, in every image set's boot
      checks): a boot of its own with swtpm attached (`tpm-tis` on X64,
      `tpm-tis-device` on AARCH64), which must reach the Shell. In the
      build, the Shell then runs `PcrRead.efi` (`tests/tpm-pcr/`, built by
      build.sh for each arch beside the firmware, out of tree, never
      shipped): TPM2_PCR_Read of SHA-256 PCRs 0-7 through
      EFI_TCG2_PROTOCOL's SubmitCommand. It must print all eight, in order,
      and its closing line; PCR0 neither zero nor the separator alone (the
      firmware measured); PCRs 3 and 6 exactly the separator alone
      (`3d458cfe…7969`, SHA-256 of 32 zero bytes and the digest of
      EV_SEPARATOR's four zero bytes), which Tcg2Dxe extends into each of
      PCRs 0-7 at ReadyToBoot. `build.sh check`, on a packed release,
      has no application: there the boot runs to the Shell only, which
      still catches this fault. swtpm must be on PATH: build.sh refuses to
      start without it, and the boot checks refuse before any boot; never
      a skip. CI's build job installs Ubuntu's `swtpm` (noble: 0.7.3, in
      main) and states its version. *To verify on CI:* Ubuntu's swtpm has
      an AppArmor profile in enforce mode (`/tmp/** rwk`, `owner
      @{HOME}/** rwk`); the control socket is under `/tmp` and the state
      under the checkout, both allowed by it, but its `unix` rules name only
      unnamed and libvirt peers, so whether it lets swtpm listen on a path
      socket is first shown by CI's run.
    - *For SoftBMC: PCRs 0-7 are stable.* On this build's AARCH64 DEBUG,
      AARCH64 RELEASE and X64 DEBUG (the same platform as above), one
      unchanged guest (one vars copy, one swtpm state directory), a cold
      boot, two warm resets (`reset`), then two more cold boots (a new QEMU
      and swtpm each): PCRs 0-7 of the SHA-256 bank identical in all five
      reads, on each set. PCRs 2, 3, 5 and 6 are the separator alone on
      both arches; PCR7 is the same on both
      (`b5710bf5…1439`, Secure Boot off); PCR1 and PCR4 differ by arch, and
      PCR0 by image (the firmware's own bytes: a rebuild of AARCH64 DEBUG
      whose bytes differed, §5 item 2, gave another PCR0).
    - *Sabotage:* this build without patch 0014 fails exactly the two
      AARCH64 TPM boots, DEBUG ("Synchronous Exception at 0x49748") and
      RELEASE (at 0x2D5A0), no Shell within 180 s, and packs nothing; X64's
      TPM boots pass. Control: with the patch, every check passes, and
      `build.sh check` on the packed tarball passes too (its TPM boots to
      the Shell on all four).
    - *`build.sh check` on an older release now fails on AARCH64:* r1's and
      r2's AARCH64 images have this fault, and the check boots every image
      set with a TPM. `check` re-checks a release this tree built, against
      its own descriptors and drivers (r2 already fails the PciSioSerialDxe
      driver check), so it is not gated.

38. **Patch 0015, `reconnect -r` with virtio-rng as the only RNG** (owner,
    2026-11-25, for r3; found by the r3 drivers work). On a CPU with no
    RNG instruction, a DEBUG image asserted in the Shell's `reconnect -r`
    whenever a NIC was present: `ASSERT [Ip4Dxe] DxeNetLib.c(944)`, after
    "Failed to locate EFI_RNG_PROTOCOL: Not Found", and the guest stopped
    (deadloop). r1 and r2 alike, and every r3 branch before this patch.
    - *Cause.* Since the CVE-2023-45237 fix, DxeNetLib's `PseudoRandom`
      takes its numbers from EFI_RNG_PROTOCOL, and the library's Depex on
      `gEfiRngProtocolGuid` keeps the network drivers from being
      dispatched until one exists. That holds only for dispatch. With
      neither RNDR (AARCH64 `cortex-a57`) nor RDRAND (X64 `qemu64`, or
      `-cpu host,-rdrand,-rdseed` under KVM), SecurityPkg's RngDxe
      installs nothing, and the only RNG is VirtioRngDxe, a driver-model
      driver on the virtio-rng PCI device. `reconnect -r` disconnects
      every driver, VirtioRngDxe's included, and connects the NIC's stack
      before virtio-rng's: Ip4Dxe, Ip6Dxe and UefiPxeBcDxe then start with
      no RNG. `PseudoRandom` returns EFI_NOT_FOUND, as documented, and
      all 20 of its call sites at the tag check that and return it. On a
      Start path (Ip4Dxe and Ip6Dxe's DriverBindingStart, Dhcp4Dxe,
      Dhcp6Dxe and Ip6Dxe's service creation, UefiPxeBcDxe's IPv6
      children) the start fails, so a later ConnectController in the same
      `reconnect -r` starts the driver again; Udp4Dxe, Udp6Dxe and TcpDxe
      call it only in their entry points, behind the Depex. But it also
      asserted. A RELEASE image, with no ASSERT compiled in, already went
      on and the network worked. With `-cpu max` (RNDR), or X64 under KVM
      with RDRAND, RngDxe is the RNG, nothing is disconnected, and nothing
      happens.
    - *The fix: drop that one ASSERT.* `PseudoRandom` keeps its
      DEBUG_ERROR line and returns EFI_NOT_FOUND. The ASSERTs on a GetRNG
      failure stay, since there an RNG exists and failed. RELEASE
      compiles no ASSERT, so RELEASE's code is unchanged; DEBUG now does
      what RELEASE did. Alternatives weighed:
      - *Ip4Dxe's (and every network driver's) Supported() refusing until
        an RNG exists*: eight drivers patched, not one line, and it still
        relies on a later connect to start them, exactly as a failing
        Start does; nothing gained.
      - *A reconnect order that brings virtio-rng first* (a Shell
        change): it fixes only the Shell's `reconnect`, not BDS, a
        `connect` of one handle, or an application's ConnectController,
        and leaves the ASSERT armed for any other order. Moving
        virtio-rng ahead of the NIC on QEMU's command line did not help
        either: r2 asserted the same way.
      - *An RNG that is always there* (RngDxe with a timer-based RngLib):
        a weaker entropy source for every network driver on every boot,
        which is what CVE-2023-45237's fix took away; not this patch's
        business.
    - *Real boards without an RNG instruction.* Nothing changes for them
      at boot: with no RNG at all, the Depex still keeps the network
      drivers from dispatching, as upstream intends (edk2 issue #9530,
      closed as invalid: OVMF's network stack now needs an RNG, such as
      `-device virtio-rng-pci`). With a
      driver-model RNG that arrives late, a network driver's start now
      fails with the same two error lines and is started by the next
      connect, as in RELEASE; it never silently stays down while an RNG
      exists and something connects the controller.
    - *Upstream.* edk2 master (`08f2a6d1`, 2026-11-26) has the same code:
      no commit since the tag touches DxeNetLib, VirtioRngDxe or the
      Shell's reconnect, and no issue or pull request found names this
      ASSERT. A candidate to send upstream.
    - *Checked on every release*: the `reconnect` boot (PROPERTIES.md,
      `boot.reconnect_network`), on all four image sets: a NIC on QEMU's
      user network (`-device virtio-net-pci,romfile=`) and `-device
      virtio-rng-pci`, AARCH64 under `cortex-a57`, X64 with
      `,-rdrand,-rdseed` added to its CPU (`host` under KVM, `max` under
      TCG); `ifconfig -l` must list eth0; `reconnect -r` must bring the
      prompt back within 60 s with no DebugLib ASSERT line on the debug
      channel; a DEBUG image must have printed "Failed to locate
      EFI_RNG_PROTOCOL" during it (else the boot tested nothing: a CPU
      model that gained an RNG, say); eth0 must be listed again and get
      QEMU's first DHCP lease, 10.0.2.15, within 30 s (`ifconfig -s eth0
      dhcp`), which needs MNP, Ip4, Udp4 and Dhcp4 started again. The
      boot has its own vars copy, since the DHCP policy is kept in NV.
      `reconnect_network: true` in each image's `measured`.
    - *Measured* (AlmaLinux 10 x86_64, QEMU 10.0.0; AARCH64 under TCG,
      X64 under KVM unless said). r2 DEBUG, both arches: no prompt after
      `reconnect -r`; the check fails with `the firmware asserted in
      \`reconnect -r\`: ASSERT [Ip4Dxe] DxeNetLib.c(944): !(((RETURN_STATUS)(Status))
      >= 0x8000000000000000ULL)` (X64: `ASSERT DxeNetLib.c(944): ...`); X64
      the same under TCG `-cpu qemu64` and `max,-rdrand,-rdseed`. r2
      RELEASE AARCH64: passes (lease in 3.8 s, `ping 10.0.2.2` 2 of 2).
      r3 with 0015, every boot check on all four image sets: no problem;
      `reconnect -r` back in 0.28-0.75 s; each DEBUG debug log holds five
      "Failed to locate EFI_RNG_PROTOCOL" lines (Ip4DriverBindingStart and
      Ip6DriverBindingStart twice, UefiPxeBcDxe once) and no ASSERT; the
      lease came in 3.8 s and `ping 10.0.2.2` answered 2 of 2.
      Sabotage: the same r3 build without 0015 (patches 0001-0005), DEBUG
      on both arches, every boot check: only the `reconnect` boot failed,
      on both, with the ASSERT line above (`reconnect_network: false`);
      every other check passed. RELEASE: AARCH64 RELEASE built with and
      without 0015 (in workspaces of different names, so the images
      differ throughout, as §4.26 found for DEBUG) has byte-identical
      `.text` in Ip4Dxe, TcpDxe and Dhcp4Dxe (each links DxeNetLib);
      their `.data` differs, holding no differing string.

## 5. Open decisions (the owner's)

1. **Release signing.** A release is checked by sha256 only. GitHub's artifact
   attestations could sign what CI built. *Recommendation:* add when a consumer
   outside the organisation fetches releases.
2. **AARCH64 reproducibility.** axlvmpool found X64 byte-reproducible and
   AARCH64 not (every build differed). At edk2-stable202611 X64 is not
   quite either: IScsiDxe's AutoGen sizes a module-scoped VOID* PCD 3 or
   4 bytes from build to build (§4.22), which changes the compressed
   `code.fd`; every other module is the same. *Recommendation:* look into it before
   anyone relies on rebuilding a release byte for byte; until then a digest
   names a build, not the source.
3. **RELEASE symbols.** Only DEBUG's symbols are published.
   *Recommendation:* add RELEASE's when a consumer debugs a RELEASE image.
4. **An uninstall command.** None yet; the install layout makes one simple
   (remove `<prefix>/<release>` and its descriptors).
5. **QEMU's flags reproduced rather than vendored** (decided by the builder in
   axlvmpool item 101; the owner may revisit).
6. **Serial Ctrl-S** (raised 2026-11-25, §4.21; **decided** by the owner
   2026-11-25: fix it in r3, patch 0007, §4.28). A serial 0x13 reaches the
   Shell with patch 0004, but the Shell pauses only for 's' with Control
   held, so serial output never pauses, as on 202602. A ShellPkg patch
   (0x13 accepted beside 's' in `NotificationFunction` and in
   `ConsoleLogger.c`'s pause loop) would make it pause; it would be the
   first patch to change the Shell's behaviour rather than restore it.
   *Recommendation:* leave it unless a consumer needs serial Ctrl-S; no
   axl-sdk test checks a pause.
7. **An intermittent virtio-gpu boot hang on CI** (raised 2026-11-25;
   **resolved** 2026-11-25: §4.25). The r2 tag's run (45708268326, e99510e)
   failed two virtio-gpu boots, each `no Shell prompt within 180 s`; other
   runs passed every boot. Each hang came within 2 s of the boot's start,
   while BDS drew on the display through VirtioGpuDxe, whose `VirtioFlush`
   polls the used ring for a synchronous control request with no time
   limit; it was never reproduced locally (AlmaLinux 10 x86_64, QEMU
   10.0.0 and, in a container, Ubuntu 26.04's QEMU 10.2.1: 67 boots).
   - *The cause*, as the record points to it (not reproduced with
     io_uring enabled). QEMU 10.2.1's io_uring main loop (`fdmon-io_uring`) can
     lose a wakeup: an event already in the completion ring is not seen,
     so the main loop sleeps until another fd event, and the guest's
     request (the boot logo's Blt) waits with it. Fixed upstream by
     961fcc0f22 ("fdmon-io_uring: check CQ ring directly in
     gsource_check", Jens Axboe, 2026-02-13) and its companion
     2ae361ef1d, both first in QEMU v10.2.2; Ubuntu 26.04's
     `1:10.2.1+ds-1ubuntu3.3` has neither. It never showed here because
     AlmaLinux 10's kernel sets `kernel.io_uring_disabled=2`, so QEMU (the
     container's too, on the same kernel) uses epoll.
   - *The evidence.* Run 45716598952 (ae29084, with the `.monitor` record)
     hung once, on `x64-release-virtio-gpu`. The record's monitor
     contact is itself an fd event: its first registers put the boot
     processor in BdsDxe (RIP 0x3EAD0B57, mapped by a rebuild of the
     image's code), the virtio-gpu queues were then drained (`inuse` 0,
     `last_avail_idx` = `shadow_avail_idx` = `used_idx`, 29 and 150), and
     the transcript goes on past the give-up to the Shell's `startup.nsh`
     countdown: the guest had waited on a request QEMU took the moment
     its main loop woke.
   - *The owner's decision* (2026-11-25): "Both". Now, CI disables io_uring
     before the boots, and states the setting and QEMU's version in its
     log (§4.25); later, the boot checks also on a fixed QEMU (item 8).
     Never a retry, which would hide a real hang in a release.
8. **The boot checks on a fixed QEMU in CI** (owner, 2026-11-25: to do,
   not built yet; item 7). A CI job that runs the boot checks on a QEMU
   with 961fcc0f22 and 2ae361ef1d (v10.2.2 or later) and io_uring left
   enabled, so the checks also cover the main loop consumers on newer
   distributions will run. *Recommendation:* when Ubuntu's runner QEMU
   reaches 10.2.2 (its stable updates), or a distribution package with
   the fixes is at hand (Debian's or Fedora's, in a container), take it
   from there rather than building QEMU in CI; otherwise build v10.2.2 (or
   the pinned latest stable) from source once per version, cached by its
   tag, `--target-list=x86_64-softmmu,aarch64-softmmu`, and point
   `QEMU_DIR` at it. The job runs `./build.sh check` on the release the
   build job uploaded (no second edk2 build), and states `sysctl
   kernel.io_uring_disabled` (0) and QEMU's version, as the build job does.
9. **The PS/2 mouse's HID: PNP0F03 or PNP0F13** (raised 2026-11-25 by the
   builder of 0006, §4.28; **decided** by the owner 2026-11-26: PNP0F13,
   changed in 0006 at integration). The inventory and the brief named PNP0F03
   (Microsoft PS/2-style mouse), which 0006 uses; QEMU's own DSDT gives
   the i8042 mouse `_HID` PNP0F13 (PS/2 port for PS/2-style mice,
   `hw/input/pckbd.c`), as the keyboard's PNP0303 matches its DSDT entry.
   Ps2MouseDxe takes either, so the firmware works the same; the device
   path (`Acpi(PNP0F03,0x0)` or `Acpi(PNP0F13,0x0)`) is what a consumer
   would match, and an OS mapping a UEFI path to ACPI would expect the
   DSDT's. *Recommendation:* PNP0F13, to match QEMU's DSDT, before r3 is
   published (a one-character change to 0006; no consumer reads the path
   yet).
10. **Serial Ctrl-S in the editors, and XOFF** (raised 2026-11-25 in review
   of 0007, §4.28). With 0007 a serial 0x13 pauses output everywhere the
   Shell's console logger writes: in `edit` and `hexedit`, whose Save is
   Ctrl-S, the key after a save is taken to resume output (as a keyboard's
   Ctrl-S did already), and a terminal or terminal server sending XOFF
   pauses the Shell until the next key. *Recommendation:* accept it, as the
   keyboard's behaviour; skipping the pause while an editor runs would be a
   further ShellPkg change.
11. **Boot checks for patches 0011 and 0013** (raised 2026-11-25, §4.33).
    The release could run `tests/repro/repro.py vfs` and `tcp` on each
    image set: about 25 s and 15 s a boot, plus building ReproPkg in the
    build and virtiofsd on CI's runner. *Recommendation:* add the `tcp`
    boot (no new host dependency; it guards the riskiest patch, which an
    upstream TcpDxe change could undo while the patch still applies), and
    leave `vfs` by hand until CI has virtiofsd.
12. **`reconnect -r` on AARCH64 with only virtio-rng ASSERTs** (found
    2026-11-25; **resolved**: the owner put the fix in r3, patch 0015,
    §4.38). On AARCH64 `cortex-a57` (no RNDR),
    the network stack's only RNG is virtio-rng; `reconnect -r` stops it
    with everything else, Ip4Dxe starts before it, and DxeNetLib asserts
    (`ASSERT [Ip4Dxe] DxeNetLib.c(944)`, "Failed to locate
    EFI_RNG_PROTOCOL"), with or without a TCP connection, on r2 and r3.
    Under `-cpu max` (RNDR) it does not happen, nor on X64 under KVM
    (RDRAND). *Recommendation:* a CONSUMERS note now; a fix (connect RNG
    drivers first, or NetLib not asserting) only if a consumer needs
    reconnect on AARCH64 without RNDR.
13. **AARCH64 with a TPM faults in PEI** (found 2026-11-25 measuring PCRs
    for SoftBMC; not fixed in r3 by this builder). r2 AARCH64 DEBUG with
    swtpm (`-chardev socket,... -tpmdev emulator,... -device
    tpm-tis-device,tpmdev=tpm0`) stops after its banner: `Synchronous
    Exception at 0x4A700`, X20 = `0x0c000000`, the virt machine's platform
    bus, where QEMU places `tpm-tis-device`. This r3 build (release
    builder's, AlmaLinux 10 x86_64) does the same (`Synchronous Exception
    at 0x49748`). Same cause as patch 0003's
    UART: since 158c1b614 PEI maps only the console UART and fw_cfg, and
    the TPM's MMIO is touched before it is mapped. X64 with `tpm-tis` is
    unaffected. *Recommendation:* map the TPM's page in
    `QemuVirtMemInfoLib` as 0003 maps the PL011s (a patch, and a TPM boot
    check per arch), when a consumer needs a TPM on AARCH64; SoftBMC asked
    about PCRs.

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
- *GitHub-hosted runners expose KVM.* The r2 runs' build jobs, on the image
  `ubuntu-latest` was then (ubuntu-26.04, 20261116.202.1), listed
  `crw-rw-rw- ... /dev/kvm` after the udev rule, so X64 booted under KVM; a
  later image may differ.
- *GitHub's runners run steps with SIGPIPE ignored.* Run 45708237045's
  no-build tests failed on `tr: write error: Broken pipe` from a `tr | awk`
  whose awk exits early; a tr killed by SIGPIPE says nothing. So no script
  here pipes a producer into a consumer that may exit before reading all of
  it (`head`, `grep -q`, an awk `exit`): under `pipefail` the producer's
  failure fails the pipeline, and where SIGPIPE is ignored it also writes to
  stderr. Reproduced locally by ignoring SIGPIPE and feeding tr its input in
  two parts, so that awk has exited before the second.
- *PCRs 0-7 of an unchanged X64 guest are stable across reboots with
  swtpm* (SoftBMC's question, 2026-11-25). r2 X64 DEBUG (r3 not yet built;
  KVM, QEMU 10.0.0, `swtpm socket --tpm2` on one state directory, `-device
  tpm-tis`), read from the Shell by a scratch application (Tcg2's
  `SubmitCommand`, `TPM2_PCR_Read`, both banks): one boot on a fresh vars
  copy, two warm `reset`s in it, three more cold boots on the same vars
  and TPM state. Every PCR 0-7 of SHA-256 and SHA-1 was the same in all
  six. SHA-256: PCR0 `2ACEE488…5404A`, PCR1 `436D805C…745EA`, PCR4
  `FE0D4678…7BA1D`, PCR7 `B5710BF5…81439`; PCR2, 3, 5, 6 the separator
  alone (`3D458CFE…E7969`). Control: r2's RELEASE image changes PCR0 (the
  firmware) and PCR4 (its Shell, the boot application) and nothing else.
  AARCH64 could not be measured: §5 item 13.

To verify:

- *The Shell's `mode`, `dmpstore` and `smbiosview` output formats* are those of
  ShellPkg's at the pin (`STR_MODE_LIST_ITEM` and so on); a new tag must be
  re-checked ([BUILDING.md](BUILDING.md), "Moving to a new edk2 tag").
- *AARCH64's DEBUG output always holds an error line* (Tcg2's "Not Found" with
  no TPM attached), which the flavour check relies on; a tag that silences it
  needs the check adjusted.
- *Ubuntu's compilers build the same behaviour as AlmaLinux's* (gcc 15 on the
  runner against 14): the property checks are the guard; the bytes differ.
  The instruction set was guarded by none of them until r2: both compilers
  default to x86-64-v3 (the runner's `amd64v3` gcc and AlmaLinux 10's), so
  X64 is now pinned to `-march=x86-64` and checked (§4.24).
