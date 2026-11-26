# Consuming a release

## The contract: QEMU firmware descriptors

An installed release is found through its QEMU firmware descriptors
(`docs/interop/firmware.json` in QEMU), in `$XDG_DATA_HOME/axl-firmware/descriptors/`
(`$HOME/.local/share/axl-firmware/descriptors` when `XDG_DATA_HOME` is unset or
not absolute). They are the same format libvirt and the distributions use, so
one selection rule serves every source of firmware:

1. Search directories in this order; a file name in an earlier directory hides
   the same name in a later one: `$XDG_DATA_HOME/axl-firmware/descriptors`,
   `$XDG_CONFIG_HOME/qemu/firmware`, `/etc/qemu/firmware`, the QEMU in use's own
   `<prefix>/share/qemu/firmware`, `/usr/share/qemu/firmware`.
2. Keep a descriptor when: `interface-types` holds `uefi`; one target has the
   arch (`x86_64`, `aarch64`) and a machine glob matching `q35` or `virt`;
   `mapping.device` is `flash`, `mode` `split` (or absent), both files `raw`
   and present; and `features` has none of `secure-boot`, `enrolled-keys`,
   `requires-smm`, `intel-tdx`, `amd-sev-snp`, `host-uefi-vars`.
3. On AARCH64 prefer a `verbose-static` one; then take the lowest file name
   (byte order).
4. Then check the drivers you need (axlvmpool requires VirtioFsDxe and
   VirtioGpuDxe), or trust a release's `manifest.json`, which lists every
   driver checked.

A release's descriptors are `10-axl-firmware-<release>-<arch>.json` (DEBUG) and
`20-axl-firmware-<release>-release-<arch>.json` (RELEASE), so the rule picks
DEBUG. To boot RELEASE, name its descriptor or its files. The firmware's own
directory, holding `Shell_<ARCH>.efi` beside `code.fd`, is the directory of the
descriptor's `executable`.

## Each consumer

| Consumer | How it finds the firmware |
|---|---|
| axlvmpool | Its discovery searches the descriptors as above (the neutral path first), checks the drivers, and records the choice. `axlvmpool firmware --arch <X64\|AARCH64> [--json]` prints `code`, `vars` and how they were chosen: **the resolver for every other consumer when axlvmpool is installed.** `axlvmpool setup` will download a release. |
| axl-sdk | `find_firmware` asks `axlvmpool firmware` when axlvmpool is installed, else applies the same descriptor rule itself. `axl firmware install` fetches a release by tag and pinned sha256 through its own toolchain installer, so SDK users need not have axlvmpool. The DEBUG X64 image is the debug OVMF its `find_debug_ovmf` needs (DESIGN §5, item 1), and the symbols tarballs serve `gdb-syms.py` and rsod-decode. |
| SoftBMC | The installed release's `Shell_<ARCH>.efi` and images, through the descriptors or `axlvmpool firmware`; for physical machines, the images `hardware_images` names (RELEASE, from r3). |
| AGT | The DEBUG images (quiet AARCH64, X64 at 1280x800) through the descriptors. |
| A downstream diagnostics project | A release fetched by tag and pinned sha256, typically RELEASE, with `bochs-display` on AARCH64 and the SMBIOS BIOS version reading the edk2 tag. |

## Fetching with a pinned sha256

Pin both the release tag and its tarball's sha256 in the consumer, as
rust-osdev's ovmf-prebuilt does:

```sh
./build.sh install --from edk2-stable202611-r3 --sha256 <the tarball's sha256>
```

Without `--sha256`, the install trusts the release's own `SHA256SUMS`. With a
tarball already on disk, `--from <path>` checks it against `SHA256SUMS` beside it
or `--sha256`. The repository is public, so a release downloads with no login from
`https://github.com/aximcode/axl-firmware/releases/download/<tag>/<asset>`; the
install uses `gh release download` when it is logged in, or the REST API with
`GITHUB_TOKEN` when that is set, and the plain URL otherwise. Every file is
checked against the release's `SHA256SUMS` (or a pinned `--sha256`), which is
the trust anchor, not the transport.

A consumer with its own fetcher reads the same three things: the asset
`axl-firmware-<release>.tar.xz`, its sha256, and the layout in
[RELEASE-FORMAT.md](RELEASE-FORMAT.md). It must rewrite the descriptors' paths
to absolute ones, as `build.sh install` does.

## Offline, or before a release is installed

With no release installed, the descriptor rule falls through to the
distribution's images and then to QEMU's bundled `edk2-*-code.fd`. Neither is
good: the distribution's lack VirtioFsDxe (and on X64 VirtioGpuDxe), and QEMU's
lack the VirtioRng fix (boots can stall; axlvmpool retries a boot once and
warns). axlvmpool names both as known bad. Install a release from a tarball
copied by hand (`--from <path> --sha256 <hex>`) where there is no network.

## Known limitations

- **AARCH64 `secondary-vga` may show nothing.** Patch 0002 skips the legacy VGA
  register accesses for `bochs-display` (an MMIO-variant display of subclass
  "other", which has no VGA and faults on `virt`); `secondary-vga` has the same
  IDs and subclass, so it is skipped too and its display may stay blank. Use
  `-device VGA`, `bochs-display`, `virtio-gpu-pci` or `ramfb` on AARCH64. X64
  is unaffected.
- **AARCH64's PCI serial port is never a console by itself.** A `-device
  pci-serial` gets an EFI_SERIAL_IO on AARCH64 from r3 (patch 0005), connected
  by BDS whether or not QEMU gives a boot order (`bootindex`), so no `connect
  -r` is needed; but it is a console only when a ConIn, ConOut or ErrOut
  variable names it. On X64 the port is a console from the start.
- **The PS/2 mouse (X64, from r3) needs a full connect, and no other mouse.**
  Patch 0006 gives QEMU's PS/2 mouse a SimplePointer at
  `PciRoot(0x0)/Pci(0x1F,0x0)/Acpi(PNP0F03,0x0)`. OVMF's BDS connects it only
  when QEMU gives no boot order; with a `bootindex`, run `connect -r` first.
  QEMU sends it events only while it is QEMU's active mouse (`info mice`): a
  USB or virtio mouse added later takes them. `-machine i8042=off` removes it.
- **The serial console's terminal type, per arch.** Both arches' serial
  consoles are VT-UTF8 from r3 (AARCH64 was VT100): DEL (0x7f) is Backspace,
  and box drawing and characters beyond ASCII go out as UTF-8. A vars file
  written by r2 or earlier keeps its VT100 ConOut entry beside the new VT-UTF8
  one; the terminal ends up VT-UTF8 all the same (measured), but start from
  the release's `vars.fd` to drop it.
- **`reconnect -r` on AARCH64 needs an RNG that is not virtio.** With
  `-cpu cortex-a57` (no RNDR) the network stack's only RNG is
  `virtio-rng-pci`, which `reconnect -r` stops too; Ip4Dxe then starts
  before it and a DEBUG image ASSERTs (`DxeNetLib.c(944)`). Use `-cpu max`
  (RNDR) on AARCH64 if a guest reconnects; X64 under KVM has RDRAND.
- **From r3, `reconnect -r` with an accepted TCP connection returns**
  (patch 0013): the connection is destroyed with its listener, its tokens
  complete EFI_ABORTED, and the peer is not told. A resident server's
  workaround (a decoy binding that destroys connections in Stop) is no
  longer needed on r3.
- **From r3, virtio-fs file calls are serialized at TPL_CALLBACK**
  (patch 0011), as FAT's are: file I/O from a timer or event callback is
  safe beside the Shell's; a call above TPL_CALLBACK ASSERTs on DEBUG.
- **No Shell fallback.** With no bootable disk, the firmware does not start the
  embedded Shell; boot your own disk, or put `Shell_<ARCH>.efi` on it.

## The console size

edk2-stable202611's text modes (as 202602's) are larger than stable202408's: TerminalDxe
gained 128x40, 160x42 and 240x56, and the console takes the largest mode all its
devices share. A serial-only console runs at 240x56. The rule for every consumer
([DESIGN.md](DESIGN.md) §4.5):

- **Never assume a console size.** A test that needs one reads it (the Shell's
  `mode`, or `ConOut->Mode`) or sets it (`mode C R`).
- **Set it after every console device has joined ConOut, and read it back.**
  axl-sdk found that a `mode C R` set before a device joins ConOut (a GOP, a
  terminal) does not survive the join: ConSplitter re-chooses the mode from what
  the devices share. Set the mode once the console is complete (in the Shell,
  or after the last `ConnectController`), then check `mode`'s current row.
- **Fit an interactive console to the user's terminal.** A launcher with a
  person watching sets `mode C R` from the host terminal's columns and rows,
  choosing the largest of the release's modes that fits. Each image's measured
  list is in its manifest (`images.<dir>.measured.text_modes.<display>.modes`),
  per display: `serial`, `std-vga`, `virtio-gpu`, `bochs-display`; so no probe
  boot is needed.
- A fixed-size terminal smaller than the guest's console (for example an ssh
  session at 132x60 against 240x56) is a mismatch the consumer must handle,
  by setting the guest's mode first.
