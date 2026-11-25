# Changelog

Each release is `<edk2 tag>-r<N>`; its section here is its release notes.

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
