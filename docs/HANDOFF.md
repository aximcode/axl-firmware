# Handoff (uncommitted working notes; delete before the release)

State on 2026-11-25, late. The candidate is CI run 45653672897 (main at the
"std-VGA without its EDID" commit). Outstanding runs: main 45650143026
(superseded), sabotage static 45650340504, bochs-ungated 45650246888,
nv-readonly 45650933400, boot 45653681305 (re-run for the GOP check),
variant-gate 45653695502 (re-run, expect AARCH64 std-VGA RED too).
Done: C boot 45650236372 RED on front page, SMBIOS, debug output (each image),
GOP not caught then -> fixed by edid=off; G text-modes 45650284789 RED;
H variant-gate 45650346796 RED on X64 std-VGA 640x480.
Patch 0003 approved by the owner (carry, no upstream report); docs updated.

## Update (second builder, 2026-11-25 16:40 UTC)

- 45650143026 (main fa45456, with 0003): GREEN. Its manifest: text modes
  serial 80x25 80x50 100x31 128x40 160x42 240x56 (current 240x56), std-VGA,
  virtio-gpu and bochs-display 80x25 100x31 128x40 160x42 (current 160x42), on
  all four images; GOP 1280x800 drawn; SMBIOS edk2-stable202611; CPU matrix all
  true both flavours; AARCH64 DEBUG second UART 1328 bytes, 0 INFO lines.
- Candidate 45653672897 (main 2980086): still running at 16:40.
- A static 45650340504 (202611 + 0003): RED on every intended check (pointer
  drivers/QemuVideoDxe/Shell lacking, SecureBootConfigDxe held, PCD not UiApp,
  mask 0x07, AARCH64 0x8000004f, RELEASE from DEBUG).
- D bochs-ungated 45650246888: RED: AARCH64 std-VGA and bochs-display boots
  "no Shell prompt within 180 s" (Synchronous Exception, ASSERT ArmCpuDxe), both flavours.
- C 45650236372 (first run, before edid=off): RED on front page, SMBIOS
  'sabotaged', X64 debugcon 1472 bytes not verbose, AARCH64 199650 bytes/170
  INFO lines, RELEASE output; GOP 1024x768 not caught -> re-run 45653681305.
- G 45650284789 RED ("x64: text modes measured ..."); H 45650346796 RED on X64
  std-VGA 640x480 (both flavours), re-run 45653695502.
- F1/F2 rebased onto main 2980086 and re-run: source-git 45654292168,
  source-stamp 45654296317 (on 202602 they were RED: 45640469736, 45640480686).
- Still running: 45653681305 (C), 45653695502 (H), 45650933400 (E nv-readonly),
  45654292168, 45654296317. Nothing committed yet; no tag.

## Where things stand

- Pin: edk2-stable202611 (`556dcfb4…`), fixes `8ba02634` (VirtioRng) and
  `b8df7d9c` (LPA2) checked as ancestors. Toolchain tag `GCC` (202611 removed
  GCC5). `SOURCE_DATE_EPOCH` = the pinned commit's date.
- Patches: 0001 pointer drivers, 0002 bochs-display video (narrow gate: off
  x86, MMIO variant, subclass "other"), 0003 map the debug PL011 in PEI
  (upstream 158c1b614 regression: AARCH64 DEBUG with two UARTs faulted;
  verified locally under cortex-a57 and max).
- CI run 45644063120 (202611 before 0003): all four builds and every static
  check passed; every boot check passed except AARCH64 DEBUG's two-UART
  serial boot (that regression). 45650143026 adds 0003.
- The tag `edk2-stable202611-r1` is HELD: report the candidate's property
  results to the coordinator first (text modes serial and std-VGA, GOP,
  SMBIOS version, the CPU-model matrix).
- Docs (README, CLAUDE.md, CHANGELOG, docs/*.md) are committed with the
  review fixes. RELEASE-FORMAT's example is run 45650143026's manifest, to be
  refreshed from the final candidate. Missing: PROPERTIES' "Sabotage runs in
  CI" section.

## Sabotage results so far

- Unit level (local, `tests/test_axlfw.py`): every check RED under its
  sabotage, restored by cp + touch, sha256 checked (see the commit messages).
- CI on the 202602 base: A static (all intended REDs, plus the Shell missing
  under SECURE_BOOT_ENABLE), F1 git-only guard ("?? OvmfPkg/stray.txt",
  "the build changed the source tree"), F2 stamp-only guard ("the build
  wrote into the source tree").
- Ready but not pushed (scratch worktree `wt`, branches `sabotage/*` on the
  pre-0003 main 850f059; rebase onto main first): static, boot,
  bochs-ungated, variant-gate (expect std-VGA "shows nothing"), nv-readonly,
  text-modes. Push each and `gh workflow run ci.yml --ref <branch>`.
- Control for the CPU-model check: axlvmpool's 202602 AARCH64 image under
  max has no Shell in 60 s; under cortex-a57 it has one.

## Next

1. 45650143026 green → read `dist` artifact's manifest: text modes, GOP,
   SMBIOS, cpu_models_reach_shell; fill RELEASE-FORMAT's example.
2. Rebase and run the sabotage branches; fill PROPERTIES; delete the
   `sabotage/*` branches on GitHub afterwards (check none remain).
3. Commit the docs; a second independent review; fix or answer.
4. Report to the coordinator; on the go-ahead, tag `edk2-stable202611-r1`,
   watch the release job, download and `sha256sum -c SHA256SUMS`, install
   with `./build.sh install --from edk2-stable202611-r1`, check the
   descriptors parse, run `$HOME/bin/axlvmpool firmware --arch X64`.
