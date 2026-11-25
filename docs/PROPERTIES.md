# Checked properties

Every release is checked for each property below before it is packed
(`build.sh build`), and again on the packed tarball as a consumer unpacks it
(`build.sh check`, in CI). A check that fails stops the build: nothing is packed.
Each check was shown to fail: by a **sabotage** (the build or the code broken on
purpose, then restored by `cp` plus `touch` and checked by sha256) or by a
**control** (a real image known to lack the property).

Static checks are in `lib/axlfw/checks.py` and `lib/axlfw/release.py`; boot
checks in `lib/axlfw/boot.py`. Each boot runs the release's own Shell from a FAT
disk, driven over the serial console; X64 under KVM where available, else TCG;
AARCH64 under TCG (`cortex-a57`).

## Images and build

| Property | How it is checked | Shown to fail by |
|---|---|---|
| Both arches, both flavours: `x64`, `aarch64`, `release/x64`, `release/aarch64`, each `code.fd`, `vars.fd` (AARCH64's vars template included) and `Shell_<ARCH>.efi` | `release.stage_images` copies each from its build; `build.sh check` and the install require all four sets in the manifest, each file at its set's path (`install.image_files`), with the manifest's size and sha256 | unit `test_corrupt_set`; tampered manifests (a file entry naming another set's file, a set missing, no sets, a key missing): `test_tampered_manifests` (install), `ReleaseCheckTests.test_tampered` (`check`) |
| AARCH64 code and vars exactly 64 MiB | padded; an image larger than its bank is refused | (refusal path; QEMU refuses other sizes at boot) |
| The drivers, by FFS GUID, in every code image (the network stack is SnpDxe, MnpDxe, Ip4Dxe, TcpDxe and Dhcp4Dxe on X64, and VirtioNetDxe, which produces SNP itself, with MnpDxe, Ip4Dxe, TcpDxe and Dhcp4Dxe on AARCH64): VirtioFsDxe, VirtioGpuDxe, VirtioRngDxe, VirtioNetDxe, VirtioBlkDxe, VirtioScsiDxe, VirtioSerialDxe, Ip6Dxe, HttpDxe, TlsDxe, IScsiDxe, MnpDxe, Ip4Dxe, TcpDxe, Dhcp4Dxe, Tcg2Dxe, UsbMouseDxe, UsbMouseAbsolutePointerDxe, QemuVideoDxe, UiApp, the Shell (`7C04A583-9E3E-4F1C-AD65-E05268D0B4D1`); on X64 also SnpDxe, PciSioSerialDxe, Ps2MouseDxe | `checks.check_image`, the FV walked by uefi_firmware (`fv.py`); each GUID compared with its INF at the tag (`source-guids`) | controls: AlmaLinux `OVMF_CODE.fd` lacks VirtioFsDxe, VirtioGpuDxe and the pointer drivers, exactly (`tests/firmware-tests.sh`, run locally on EL: CI's Ubuntu runners have no such image, so there the control SKIPs, while the source and installed-image checks are required); axlvmpool's pre-patch stable202602 images lack the pointer drivers (and AARCH64 QemuVideoDxe); sabotage: patch 0001 removed (CI, below); unit: the missing-driver list dropped |
| No Secure Boot: SecureBootConfigDxe (`F0E6A44F-7195-41C3-AC64-54F202CD0A21`) absent; no `secure-boot` feature | `checks.check_image`; the walk's required drivers, in the same DXE volume, prove the volume was decoded | control: AlmaLinux `OVMF_CODE.secboot.fd` "holds: SecureBootConfigDxe" (`tests/firmware-tests.sh`, run locally on EL, not in CI); unit: the absent list dropped; sabotage: `SECURE_BOOT_ENABLE=TRUE` (CI) |
| The Shell beside each image is a PE32+ EFI application for its arch | `checks.check_shell` | unit `test_shells` (wrong machine, PE32, subsystem, not PE) |
| The flavour: DEBUG compiled with ASSERTs and DEBUG (no `MDEPKG_NDEBUG`), RELEASE without | DxeMain's Makefile and the build directory (`checks.build_facts`) | unit `test_release_needs_ndebug`, `test_flavour_swapped`; sabotage: RELEASE images taken from the DEBUG build (CI) |
| QEMU's per-arch DEBUG level: X64 `PcdDebugPrintErrorLevel` 0x8000004F, AARCH64 0x80000000 | DxeMain's AutoGen.c | unit `test_aarch64_print_level`; sabotage: AARCH64 at 0x8000004F (CI) |
| A debug OVMF: X64 DEBUG's `PcdDebugPropertyMask` has ASSERT (0x01) with deadloop (0x20) or breakpoint (0x10), and CLEAR_MEMORY (0x08) | DxeMain's AutoGen.c; stated as `debug.assert`, `debug.clear_memory` | unit `test_debug_x64_without_clear_memory`; sabotage: the mask at 0x07 (CI) |
| The front-page PCD: `PcdBootManagerMenuFile` = UiApp on all four | BdsDxe's AutoGen | unit `test_boot_manager_menu_app`; sabotage: the PCD dropped (CI) |
| The descriptors, exactly (features per arch and flavour, `10-`/`20-` names) | `release.descriptor`; `build.sh check` compares each packed descriptor | unit `DescriptorTests` against hand-written `tests/fixtures/descriptors.json`; unit: the features dropped |
| Symbols per arch: `DxeCore.debug`, `BdsDxe.debug`, `Shell.debug` as ELF, and FV maps | `release.check_symbols` before packing | unit `test_symbols`; unit: the ELF check dropped |
| The source untouched by the build | `git status --porcelain --ignored` of the tree and submodules, before and after; the fetched source read-only | sabotage: a file written into the source during the build (CI) |

## Booted

| Property | How it is checked | Shown to fail by |
|---|---|---|
| The front page after the Shell's `exit` | std-VGA boot: `exit`, then "Boot Maintenance Manager" on the console | sabotage: `PcdBootManagerMenuFile` dropped (CI) |
| The default GOP mode is 1280x800 (X64, as asked; AARCH64 too) | std-VGA boot with `-device VGA,edid=off`: a screendump's size at the Shell. Without `edid=off`, QemuVideoDxe takes the EDID's preferred mode, and QEMU's default EDID also asks 1280x800, so a plain `-device VGA` gives 1280x800 either way | sabotage: `PcdVideoHorizontalResolution`/`Vertical` at 1024x768 went unnoticed while the boot kept the EDID (CI run 45650236372), which is why the check turns it off; re-run on the final check (CI) |
| `bochs-display` gets a working GOP on both arches (patch 0002 on AARCH64), at the firmware's default mode | a `-device bochs-display,edid=off` boot: `drivers` shows "QEMU Video Driver" managing a device, and a screendump is 1280x800 with a lit pixel. Without `edid=off`, QemuVideoDxe takes the EDID's preferred mode for the whole MMIO variant (`QemuVideoBochsEdid`), so the size measured QEMU: on run 45653672897's images, `xres=1024,yres=768` gave 1024x768 with the EDID and 1280x800 with `edid=off` (DESIGN §4.8) | sabotage: patch 0002 without its PCD and VGA gating (CI) |
| std-VGA is drawn on both arches (patch 0002 leaves it alone) | the std-VGA boot's screendump has a lit pixel | unit `test_screendump`, and the lit test dropped; sabotage: the downstream reference's variant-wide gate (CI) |
| AARCH64 reaches the Shell under `-cpu max`, `cortex-a57` and `neoverse-v1`, both flavours (`max` has FEAT_LPA2: edk2 issue #11962) | a serial boot per CPU model, to the Shell's prompt | control: axlvmpool's edk2-stable202602 AARCH64 image stops after its banner under `max` (no Shell within 60 s) and reaches the Shell under `cortex-a57` |
| AARCH64 DEBUG with two PL011s (the console and the debug UART, as axl-sdk's `qemu_fw_debug_uart` runs it) reaches the Shell, the second UART carrying errors-only output (patch 0003) | the serial boot of every AARCH64 image runs with `-serial` twice: the Shell's prompt on the first, and on the second bytes with no `Loading driver at` line | control: edk2-stable202611 without patch 0003 (CI run 45644063120): "Synchronous Exception at 0x20CA0" after the banner, no Shell within 180 s, 0 bytes on the second UART |
| NV variables survive a reset | serial boot: `setvar -nv` a random token, `reset`, `dmpstore` reads it back; a new QEMU on the same vars file reads it again | sabotage: the vars flash read-only (CI) |
| SMBIOS type 0's BIOS version is the edk2 tag | serial boot: `smbiosview -t 0`'s `BIOS Version` | sabotage: another `PcdFirmwareVersionString` (CI) |
| The DEBUG flavour's output (patch 0003 makes AARCH64's second UART usable on 202611): X64 verbose on debugcon 0x402 (INFO lines); AARCH64 errors only on the second UART (bytes, no INFO line); RELEASE nothing on either | the serial boot's debugcon file or second UART | control: 202611 without patch 0003, whose AARCH64 DEBUG faults with two UARTs (CI run 45644063120, and reproduced locally); sabotage: X64 at errors only, AARCH64 at 0x8000004F, RELEASE images from the DEBUG build (CI) |
| The console text modes per arch and display (`serial`, `std-vga`, `virtio-gpu`, `bochs-display`) | the Shell's `mode` in each boot; stated in the manifest; `build.sh check` re-measures and compares | unit `test_mode` (and the parse check dropped); sabotage: a manifest whose modes differ, re-checked (CI) |

## Sabotage runs in CI

<!-- filled from the runs -->
