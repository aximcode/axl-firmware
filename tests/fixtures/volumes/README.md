# Stand-in firmware images

Copied from axlvmpool's `tests/fixtures/firmware-volumes/` (commit 185abe6),
where they are canonical; each is a few hundred bytes. `tests/test_axlfw.py`
asserts the FFS file GUIDs the walker (`lib/axlfw/fv.py`) finds in each.

- `both.fd`: one plain volume holding VirtioFsDxe and VirtioGpuDxe.
- `both-lzma.fd`: laid out as OVMF: an outer volume, an LZMA section, the
  DXE volume with the DXE core, the UEFI Shell, then both drivers.
- `no-virtiofs.fd`, `no-gpu.fd`, `neither.fd`: as `both-lzma.fd`, without
  VirtioFsDxe, VirtioGpuDxe, or either.
- `not-a-volume.fd`: text.
