# Changelog

Each release is `<edk2 tag>-r<N>`; its section here is its release notes.

## edk2-stable202611-r3

A second serial port on AARCH64, and a manifest field naming the images
for real hardware. Everything else is as in r2.

- **`0005-pci-serial.patch`**: ArmVirtQemu now builds PciSioSerialDxe,
  so an AARCH64 guest given a PCI 16550 UART (QEMU's `-device
  pci-serial,chardev=...`) gets a second EFI_SERIAL_IO beside the
  PL011's, as X64 guests already did. The console stays on the PL011:
  the new port is a console only when a ConIn, ConOut or ErrOut variable
  names it. X64 is built as in r2 (the patch touches only ArmVirtPkg).
  The driver check now requires PciSioSerialDxe on both arches.
  ArmVirtQemu's BDS now connects the port even when QEMU gives a boot
  order (a `bootindex`), so it has its EFI_SERIAL_IO without a `connect -r`.
- **Console and input fixes** (r3 console):
  - `0006-ps2-mouse-sio.patch`: X64's PS/2 mouse works: SioBusDxe gains a
    PNP0F03 child, so Ps2MouseDxe binds (`Acpi(PNP0F03,0x0)`); QEMU sends it
    events while no USB or virtio mouse is its active one.
  - `0007-shell-ctrl-s.patch`: a serial Ctrl-S (0x13) pauses the Shell's
    output, and the next key resumes it, as a keyboard's Ctrl-S does (in
    `edit` and `hexedit` too, and for a terminal's XOFF). A new boot
    check, `ctrl-s`, on all four image sets.
  - `0008-terminal-widechar.patch`: on a VT100 (or PC-ANSI, TTY ...)
    terminal, a character beyond ASCII is written as `?`, not as its low
    byte (U+0141 was `A`, U+010D a carriage return).
  - `0009-shell-delete-absent.patch`: the Shell's `DeleteFileByName` of a
    file that does not exist returns `EFI_NOT_FOUND`, where it created
    the file, deleted it and returned success (a virtiofs host saw both).
  - `0010-virtio-mouse-wheel.patch`: `virtio-mouse-pci`'s wheel reaches
    `RelativeMovementZ`, and its `ResolutionZ` is 8.
  - **AARCH64's serial console is VT-UTF8**, as X64's: DEL (0x7f) is
    Backspace (on r2 it was Delete, so `abX`, DEL, `c` gave `abXc`), and
    box drawing goes out as UTF-8. The measured text modes are unchanged.
    The boot check now types a DEL at the prompt on all four image sets.
  - A new static check: every patch the release requires was applied,
    the patched copy is the source with the patches applied, and its C
    was compiled from that copy (`build.sh check`: each is listed in `edk2.patches`).
- **`hardware_images`** in `manifest.json`: per arch, the image to ship
  to real hardware, where a DEBUG build's ASSERT can halt the machine:
  `{"x64": "release/x64", "aarch64": "release/aarch64"}`. Checked: a
  release whose entry names a missing image, another arch's, or one not
  RELEASE is not packed. `default_flavour` stays `DEBUG`, and the
  manifest stays format 2: no existing field was removed or reshaped
  (`edk2.patches` lists 0005, and AARCH64's `verified.drivers`
  PciSioSerialDxe).
- Each new tag re-checks every patch against upstream.

## edk2-stable202611-r2

Two fixes for r1: a serial Ctrl-C reached the UEFI Shell no more, and
r1's X64 images need an x86-64-v3 CPU. Everything else is as in r1.

- **r1's X64 images need an x86-64-v3 CPU** (Haswell or later, or a TCG
  CPU model with BMI1, BMI2, MOVBE and LZCNT, such as `-cpu max`). They
  were built on GitHub's Ubuntu 26.04 runner, whose gcc defaults to
  x86-64-v3, and edk2's X64 flags name no `-march`: SEC, PEI, DXE and the
  Shell hold about two thousand BMI1, BMI2, MOVBE and LZCNT instructions,
  and on an older CPU, or under TCG with `-cpu qemu64` or `Nehalem`, r1
  stops at once on "X64 Exception Type - 06(#UD - Invalid Opcode)". Under
  KVM on a v3 host, as in r1's own checks, it boots. AARCH64 is not
  affected.
- **X64 is now built for baseline x86-64**: `-march=x86-64` on edk2's X64
  C flags, in the build's own `tools_def.txt` (no edk2 file changes).
  A new check, before packing, holds every X64 module of each image (SEC,
  PEI, DXE, the Shell) to it twice: its makefiles compile with
  `-march=x86-64`, and its code, disassembled, holds no instruction of
  x86-64-v2, -v3 or -v4 (CMPXCHG16B, LAHF/SAHF, POPCNT, SSE3, SSSE3,
  SSE4.1/4.2, BMI1, BMI2, LZCNT, MOVBE, AVX, AVX2, FMA, F16C, AVX-512),
  nor of AES-NI, PCLMULQDQ, SHA, ADX, GFNI or RDSEED.
  It fails on a build without the pin.
- **CI's boots on epoll, not io_uring.** An intermittent virtio-gpu boot
  hang on CI points, by the state QEMU was left in, to the runner's QEMU
  (Ubuntu's 10.2.1), whose io_uring main loop can lose a wakeup (fixed
  upstream in QEMU 10.2.2, 961fcc0f22 and 2ae361ef1d), not to the
  firmware. CI now
  sets `kernel.io_uring_disabled=2` before the boots and states the
  setting and QEMU's version in its log. A consumer that boots on
  Ubuntu 26.04's QEMU 10.2.1 with io_uring allowed may meet the same hang,
  with any firmware.

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
