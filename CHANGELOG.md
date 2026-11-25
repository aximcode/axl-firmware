# Changelog

Each release is `<edk2 tag>-r<N>`; its section here is its release notes.

## edk2-stable202611-r2

A fix for a regression in r1: a serial Ctrl-C reached the UEFI Shell no
more. Everything else is as in r1.

- **The regression.** On r1, both arches and both flavours, a Ctrl-C typed on
  the serial console (0x03) did not stop a running Shell command or script:
  the prompt came back only when it ended. edk2-stable202602 broke within a
  fraction of a second. The cause is upstream `184fb6b1035e`
  ("MdeModulePkg/TerminalDxe: Fix handling of shift state in
  notifications", issue #12281, first in edk2-stable202608): TerminalDxe now
  fires a key notification only when the registration's shift state equals
  the key's. The Shell registers its break (0x03) and pause (0x13) with
  Control held, and a terminal sends those characters with no shift state,
  so none matched. Upstream accepted that shift-state notifications never
  fire over a serial line; no upstream fix exists for the Shell's.
- **`0004-terminal-control-keys.patch`**, in TerminalDxe's
  `IsKeyRegistered`: a key with no shift state that is a control character
  0x01-0x1A (Ctrl+A to Ctrl+Z, as a terminal sends them) matches a
  registration whose shift state is only Control (left, right or both),
  except Backspace, Tab, LF and CR (0x08, 0x09, 0x0A, 0x0D): a terminal
  sends those for keys of their own, and plain typing with Enter must
  not fire a Ctrl+M registration. Every other case keeps #12281's exact
  match, so RCtrl+n still does not fire on a plain 'n'. It serves every
  registrant of a Ctrl key, not only the Shell. The owner decided
  (2026-11-25) to carry it here, with no upstream report; each new tag
  re-checks it. A host test compiles the patched function and checks the
  exact set of characters it matches.
- **A new boot check**, on every image set: a Shell script ticking for a
  minute, broken by a serial 0x03, must bring the prompt back within 10 s
  (0.10 s measured), with no restart, and the Shell must then run a
  command. It fails on r1.
- **Ctrl-S over serial still does not pause output**, and it did not on
  edk2-stable202602 either: the Shell's notification function acts on a
  pause only for 's' with Control held, never for 0x13. The patch delivers
  0x13 to it; the Shell ignores it.
- **A licence list per image** (`images.<dir>.licenses` in the
  manifest): the licence texts that image needs, each `{path, chosen}`,
  so a consumer that vendors some images copies only theirs. edk2's
  licence always; then every third-party part compiled into a module of
  the image, derived from edk2's build report and checked against the
  objects the build compiled. X64: edk2 and OpenSSL. AARCH64: edk2,
  OpenSSL, libfdt (BSD-2-Clause chosen, GPL not) and libtl (MIT chosen,
  GPL-2.0-or-later not). The manifest stays format 2.
- **A second reading of every image.** Before packing, edk2's own VolInfo
  (from the build) lists each code image's files again, and the set must
  equal the FV walker's (uefi_firmware): two independent parsers agree.
  150 files on X64, 123 on AARCH64.
- Each new tag re-checks all four patches against upstream.

## edk2-stable202611-r1

The first release.

- edk2 `edk2-stable202611` (`556dcfb466e9f05affeaff881d738807f8156d3c`), with
  the VirtioRng fix `8ba02634` and the LPA2 fix `b8df7d9c`: AARCH64 boots under
  `-cpu max` (202511 to 202602 hung there after the banner, edk2 issue #11962).
- X64 (OvmfPkgX64, q35) and AARCH64 (ArmVirtQemu, virt), each DEBUG (the
  default; QEMU's bundled debug flavour) and RELEASE, with QEMU v10.0.0's build
  flags.
- Patches: `0001-pointer-drivers.patch` (UsbMouseDxe and
  UsbMouseAbsolutePointerDxe on both arches, Ps2MouseDxe on X64);
  `0002-bochs-display-video.patch` (QemuVideoDxe for `bochs-display` on
  AARCH64).
- `0003-map-debug-uart.patch`: AARCH64 DEBUG with a second UART (its debug
  output) boots again; upstream 202611 faulted in PEI (`158c1b614`, FAR
  0x9040030). Carried here, not reported upstream.
- Each new tag re-checks all three patches against upstream and drops any
  upstream has fixed.
- The front page after the Shell's `exit` (`PcdBootManagerMenuFile` = UiApp).
- The firmware version (SMBIOS type 0's BIOS version) is `edk2-stable202611`.
- Text modes measured per arch and display, in the manifest.
- DEBUG symbols per arch in separate tarballs.
