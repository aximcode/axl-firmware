## @file
# The guarded image's boot check: a UEFI application that writes one byte
# past a pool buffer (lib/axlfw/guard.py, docs/DESIGN.md §4.34). Built by
# build.sh against the pinned edk2, out of tree, for X64 and AARCH64.
##

[Defines]
  PLATFORM_NAME           = AxlFwGuard
  PLATFORM_GUID           = 5b0a7c1e-3f64-4d2a-9e8b-0c7d1f2a6b39
  PLATFORM_VERSION        = 1.0
  DSC_SPECIFICATION       = 0x00010005
  OUTPUT_DIRECTORY        = Build/AxlFwGuard
  SUPPORTED_ARCHITECTURES = X64|AARCH64
  BUILD_TARGETS           = DEBUG
  SKUID_IDENTIFIER        = DEFAULT

!include MdePkg/MdeLibs.dsc.inc

[LibraryClasses]
  UefiApplicationEntryPoint|MdePkg/Library/UefiApplicationEntryPoint/UefiApplicationEntryPoint.inf
  UefiBootServicesTableLib|MdePkg/Library/UefiBootServicesTableLib/UefiBootServicesTableLib.inf
  UefiRuntimeServicesTableLib|MdePkg/Library/UefiRuntimeServicesTableLib/UefiRuntimeServicesTableLib.inf
  UefiLib|MdePkg/Library/UefiLib/UefiLib.inf
  BaseLib|MdePkg/Library/BaseLib/BaseLib.inf
  BaseMemoryLib|MdePkg/Library/BaseMemoryLib/BaseMemoryLib.inf
  PrintLib|MdePkg/Library/BasePrintLib/BasePrintLib.inf
  PcdLib|MdePkg/Library/BasePcdLibNull/BasePcdLibNull.inf
  DebugLib|MdePkg/Library/BaseDebugLibNull/BaseDebugLibNull.inf
  MemoryAllocationLib|MdePkg/Library/UefiMemoryAllocationLib/UefiMemoryAllocationLib.inf
  DevicePathLib|MdePkg/Library/UefiDevicePathLib/UefiDevicePathLib.inf

[Components]
  AxlFwGuardPkg/Overrun/Overrun.inf
