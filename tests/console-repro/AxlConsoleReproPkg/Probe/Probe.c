/** @file
  Probe.efi: what the r3 console reproducers (tests/console-repro/repro.py)
  read from inside the guest, printed one fact per line.

    Probe.efi pointer SECONDS
      Each EFI_SIMPLE_POINTER_PROTOCOL with a device path (the console
      splitter's own, which has none, is skipped: reading it would drain the
      others): "pointer N: <path> res X,Y,Z", then "pointer ready"; then for
      SECONDS every state read: "pointer N: x=.. y=.. z=.. l=.. r=..";
      then "pointer done".
    Probe.efi delete PATH
      EFI_SHELL_PROTOCOL.DeleteFileByName (PATH):
      "delete <PATH>: <status> (0x..)", then whether PATH opens afterwards:
      "exists after: yes" or "exists after: no (<status>)".
    Probe.efi wide
      Every serial port's terminal switched to VT100 (ConnectController with
      a VenVt100 node, as TerminalDxe allows), then "wide<" U+0141 U+0158 ">"
      written to the console: a VT100 terminal writes each as '?', or, with
      the CHAR8 cast unchecked, as 'A' and 'X'.

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <Uefi.h>
#include <Guid/PcAnsi.h>
#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DevicePathLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>
#include <Protocol/SerialIo.h>
#include <Protocol/Shell.h>
#include <Protocol/ShellParameters.h>
#include <Protocol/SimplePointer.h>

typedef struct {
  VENDOR_DEVICE_PATH          Vendor;
  EFI_DEVICE_PATH_PROTOCOL    End;
} TERMINAL_TYPE_PATH;

STATIC
EFI_STATUS
Pointer (
  IN UINTN  Seconds
  )
{
  EFI_STATUS                   Status;
  EFI_HANDLE                   *Handles;
  UINTN                        Count;
  UINTN                        Index;
  UINTN                        Tick;
  EFI_SIMPLE_POINTER_PROTOCOL  **Pointers;
  EFI_DEVICE_PATH_PROTOCOL     *Path;
  CHAR16                       *Text;
  EFI_SIMPLE_POINTER_STATE     State;

  Status = gBS->LocateHandleBuffer (ByProtocol, &gEfiSimplePointerProtocolGuid, NULL, &Count, &Handles);
  if (EFI_ERROR (Status)) {
    Print (L"pointer none: %r\n", Status);
    Count   = 0;
    Handles = NULL;
  }

  Pointers = AllocateZeroPool ((Count + 1) * sizeof (*Pointers));
  if (Pointers == NULL) {
    return EFI_OUT_OF_RESOURCES;
  }

  for (Index = 0; Index < Count; Index++) {
    Path = DevicePathFromHandle (Handles[Index]);
    if (Path == NULL) {
      continue;
    }

    gBS->HandleProtocol (Handles[Index], &gEfiSimplePointerProtocolGuid, (VOID **)&Pointers[Index]);
    Text = ConvertDevicePathToText (Path, FALSE, FALSE);
    Print (
      L"pointer %u: %s res %lu,%lu,%lu\n",
      (UINT32)Index,
      Text == NULL ? L"?" : Text,
      Pointers[Index]->Mode->ResolutionX,
      Pointers[Index]->Mode->ResolutionY,
      Pointers[Index]->Mode->ResolutionZ
      );
    if (Text != NULL) {
      FreePool (Text);
    }

    Pointers[Index]->Reset (Pointers[Index], FALSE);
  }

  Print (L"pointer ready\n");
  for (Tick = 0; Tick < Seconds * 100; Tick++) {
    for (Index = 0; Index < Count; Index++) {
      if (Pointers[Index] == NULL) {
        continue;
      }

      ZeroMem (&State, sizeof (State));
      if (!EFI_ERROR (Pointers[Index]->GetState (Pointers[Index], &State))) {
        Print (
          L"pointer %u: x=%d y=%d z=%d l=%d r=%d\n",
          (UINT32)Index,
          State.RelativeMovementX,
          State.RelativeMovementY,
          State.RelativeMovementZ,
          State.LeftButton,
          State.RightButton
          );
      }
    }

    gBS->Stall (10000);
  }

  Print (L"pointer done\n");
  FreePool (Pointers);
  if (Handles != NULL) {
    FreePool (Handles);
  }

  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
Delete (
  IN CONST CHAR16  *Name
  )
{
  EFI_STATUS          Status;
  EFI_SHELL_PROTOCOL  *Shell;
  SHELL_FILE_HANDLE   File;

  Status = gBS->LocateProtocol (&gEfiShellProtocolGuid, NULL, (VOID **)&Shell);
  if (EFI_ERROR (Status)) {
    Print (L"delete: no EFI_SHELL_PROTOCOL: %r\n", Status);
    return Status;
  }

  Status = Shell->DeleteFileByName (Name);
  Print (L"delete %s: %r (0x%lx)\n", Name, Status, (UINT64)Status);
  Status = Shell->OpenFileByName (Name, &File, EFI_FILE_MODE_READ);
  if (EFI_ERROR (Status)) {
    Print (L"exists after: no (%r)\n", Status);
  } else {
    Print (L"exists after: yes\n");
    Shell->CloseFile (File);
  }

  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
Wide (
  VOID
  )
{
  EFI_STATUS          Status;
  EFI_HANDLE          *Handles;
  UINTN               Count;
  UINTN               Index;
  TERMINAL_TYPE_PATH  Vt100;

  Vt100.Vendor.Header.Type    = MESSAGING_DEVICE_PATH;
  Vt100.Vendor.Header.SubType = MSG_VENDOR_DP;
  SetDevicePathNodeLength (&Vt100.Vendor.Header, sizeof (Vt100.Vendor));
  CopyGuid (&Vt100.Vendor.Guid, &gEfiVT100Guid);
  SetDevicePathEndNode (&Vt100.End);

  Status = gBS->LocateHandleBuffer (ByProtocol, &gEfiSerialIoProtocolGuid, NULL, &Count, &Handles);
  if (EFI_ERROR (Status)) {
    Print (L"wide: no serial port: %r\n", Status);
    return Status;
  }

  for (Index = 0; Index < Count; Index++) {
    Status = gBS->ConnectController (Handles[Index], NULL, (EFI_DEVICE_PATH_PROTOCOL *)&Vt100, FALSE);
    Print (L"vt100 %u: %r\n", (UINT32)Index, Status);
  }

  FreePool (Handles);
  Status = gST->ConOut->OutputString (gST->ConOut, L"wide<\x0141\x0158>\r\n");
  Print (L"wide written: %r\n", Status);
  return EFI_SUCCESS;
}

/**
  The entry point: one of the commands above, by its first argument.

  @param[in] ImageHandle  The image's handle.
  @param[in] SystemTable  The system table.

  @retval EFI_SUCCESS            The command ran.
  @retval EFI_INVALID_PARAMETER  No command, or an unknown one.
**/
EFI_STATUS
EFIAPI
ProbeMain (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_STATUS                     Status;
  EFI_SHELL_PARAMETERS_PROTOCOL  *Params;

  Status = gBS->HandleProtocol (ImageHandle, &gEfiShellParametersProtocolGuid, (VOID **)&Params);
  if (EFI_ERROR (Status)) {
    Print (L"probe: run from the Shell\n");
    return EFI_INVALID_PARAMETER;
  }

  if ((Params->Argc == 3) && (StrCmp (Params->Argv[1], L"pointer") == 0)) {
    return Pointer (StrDecimalToUintn (Params->Argv[2]));
  }

  if ((Params->Argc == 3) && (StrCmp (Params->Argv[1], L"delete") == 0)) {
    return Delete (Params->Argv[2]);
  }

  if ((Params->Argc == 2) && (StrCmp (Params->Argv[1], L"wide") == 0)) {
    return Wide ();
  }

  Print (L"usage: probe pointer SECONDS | delete PATH | wide\n");
  return EFI_INVALID_PARAMETER;
}
