/** @file
  The guarded image's boot check (lib/axlfw/guard.py): allocate a pool
  buffer of OVERRUN_SIZE bytes of EfiBootServicesData, a multiple of 8, and
  write one byte past its end.

  Under the UEFI pool guard with the tail direction (PcdHeapGuardPropertyMask
  BIT1, BIT7 clear) a buffer whose size is a multiple of 8 ends exactly at
  its tail guard page, so the write faults. Without the guard it lands in the
  pool's own tail and nothing notices: the buffer is never freed, so the
  DEBUG pool's tail-signature ASSERT on free is not reached either.

  It prints OVERRUN_BEFORE before the write and OVERRUN_AFTER after it; a
  guarded image shows only the first, then the CPU exception.
**/

#include <Uefi.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>

#define OVERRUN_SIZE  64

EFI_STATUS
EFIAPI
UefiMain (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_STATUS       Status;
  VOID             *Buffer;
  volatile UINT8   *Bytes;
  volatile UINTN   Size;

  Size   = OVERRUN_SIZE;
  Status = gBS->AllocatePool (EfiBootServicesData, Size, &Buffer);
  if (EFI_ERROR (Status)) {
    Print (L"axlfw-overrun: AllocatePool failed: %r\n", Status);
    return Status;
  }

  Bytes = Buffer;
  Print (L"axlfw-overrun: writing byte %u of a %u-byte pool buffer\n", (UINT32)Size, (UINT32)Size);
  Bytes[Size] = 0x5A;
  Print (L"axlfw-overrun: written, no fault\n");
  return EFI_SUCCESS;
}
