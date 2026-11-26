## @file
#  Builds Probe.efi, the r3 console reproducers' guest application, for X64
#  and AARCH64 (tests/console-repro/repro.py builds it with the release
#  build's own BaseTools).
#
#  SPDX-License-Identifier: BSD-2-Clause-Patent
##

[Defines]
  PLATFORM_NAME           = AxlConsoleRepro
  PLATFORM_GUID           = 8E31C5D7-6A2F-4B19-B4D3-7C0E9F2A5B48
  PLATFORM_VERSION        = 1.0
  DSC_SPECIFICATION       = 0x00010005
  OUTPUT_DIRECTORY        = Build/AxlConsoleRepro
  SUPPORTED_ARCHITECTURES = X64|AARCH64
  BUILD_TARGETS           = RELEASE
  SKUID_IDENTIFIER        = DEFAULT

!include MdePkg/MdeLibs.dsc.inc

[LibraryClasses]
  UefiApplicationEntryPoint|MdePkg/Library/UefiApplicationEntryPoint/UefiApplicationEntryPoint.inf
  UefiLib|MdePkg/Library/UefiLib/UefiLib.inf
  UefiBootServicesTableLib|MdePkg/Library/UefiBootServicesTableLib/UefiBootServicesTableLib.inf
  UefiRuntimeServicesTableLib|MdePkg/Library/UefiRuntimeServicesTableLib/UefiRuntimeServicesTableLib.inf
  MemoryAllocationLib|MdePkg/Library/UefiMemoryAllocationLib/UefiMemoryAllocationLib.inf
  DevicePathLib|MdePkg/Library/UefiDevicePathLib/UefiDevicePathLib.inf
  BaseLib|MdePkg/Library/BaseLib/BaseLib.inf
  BaseMemoryLib|MdePkg/Library/BaseMemoryLib/BaseMemoryLib.inf
  PrintLib|MdePkg/Library/BasePrintLib/BasePrintLib.inf
  PcdLib|MdePkg/Library/BasePcdLibNull/BasePcdLibNull.inf
  DebugLib|MdePkg/Library/BaseDebugLibNull/BaseDebugLibNull.inf

[Components]
  AxlConsoleReproPkg/Probe/Probe.inf

[BuildOptions]
  # Baseline x86-64, as the release's X64 code (DESIGN §4.24): the host gcc
  # may default to x86-64-v3, and OVMF does not enable AVX (CR4.OSXSAVE).
  GCC:*_*_X64_CC_FLAGS = -march=x86-64
