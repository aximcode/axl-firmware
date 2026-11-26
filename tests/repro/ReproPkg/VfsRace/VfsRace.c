/** @file
  VfsRace: file I/O on the volume this application was loaded from, in the
  foreground and, at the same time, from a 1 ms periodic timer at
  TPL_CALLBACK, which preempts the foreground's calls.

  Every round (foreground or timer) opens the volume, opens race.txt, reads
  it whole, gets its EFI_FILE_INFO, and closes both: about seven requests on
  a virtio-fs volume. Each read must equal the first read.

  Prints "vfsrace: PASS fg=<n> cb=<n>" after VFSRACE_CALLBACKS timer rounds
  with every round correct, or "vfsrace: FAIL ..." naming the bad rounds.
  A driver that is not re-entrant hangs instead (no line at all).
**/

#include <Uefi.h>
#include <Guid/FileInfo.h>
#include <Library/BaseMemoryLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>
#include <Protocol/LoadedImage.h>
#include <Protocol/SimpleFileSystem.h>

#define VFSRACE_FILE       L"race.txt"
#define VFSRACE_MAX        4096
#define VFSRACE_CALLBACKS  1000

typedef struct {
  UINT32        Rounds;
  UINT32        Bad;
  EFI_STATUS    FirstError;
  UINT8         Buffer[VFSRACE_MAX];
  UINT8         Info[SIZE_OF_EFI_FILE_INFO + 512];
} VFSRACE_SIDE;

STATIC EFI_SIMPLE_FILE_SYSTEM_PROTOCOL  *mFs;
STATIC UINT8                            mReference[VFSRACE_MAX];
STATIC UINTN                            mReferenceSize;
STATIC VFSRACE_SIDE                     mFg;
STATIC VFSRACE_SIDE                     mCb;
STATIC volatile BOOLEAN                 mStop;

STATIC
EFI_STATUS
ReadOnce (
  IN OUT VFSRACE_SIDE  *Side,
  OUT UINTN            *Size
  )
{
  EFI_STATUS         Status;
  EFI_STATUS         CloseStatus;
  EFI_FILE_PROTOCOL  *Root;
  EFI_FILE_PROTOCOL  *File;
  UINTN              InfoSize;

  Status = mFs->OpenVolume (mFs, &Root);
  if (EFI_ERROR (Status)) {
    return Status;
  }

  Status = Root->Open (Root, &File, VFSRACE_FILE, EFI_FILE_MODE_READ, 0);
  if (!EFI_ERROR (Status)) {
    *Size  = VFSRACE_MAX;
    Status = File->Read (File, Size, Side->Buffer);
    if (!EFI_ERROR (Status)) {
      InfoSize = sizeof Side->Info;
      Status   = File->GetInfo (File, &gEfiFileInfoGuid, &InfoSize, Side->Info);
      if (!EFI_ERROR (Status) &&
          (((EFI_FILE_INFO *)Side->Info)->FileSize != *Size))
      {
        Status = EFI_VOLUME_CORRUPTED;
      }
    }

    CloseStatus = File->Close (File);
    if (!EFI_ERROR (Status)) {
      Status = CloseStatus;
    }
  }

  CloseStatus = Root->Close (Root);
  if (!EFI_ERROR (Status)) {
    Status = CloseStatus;
  }

  return Status;
}

STATIC
VOID
Round (
  IN OUT VFSRACE_SIDE  *Side
  )
{
  EFI_STATUS  Status;
  UINTN       Size;

  Size   = 0;
  Status = ReadOnce (Side, &Size);
  if (!EFI_ERROR (Status) &&
      ((Size != mReferenceSize) || (CompareMem (Side->Buffer, mReference, Size) != 0)))
  {
    Status = EFI_CRC_ERROR;
  }

  if (EFI_ERROR (Status)) {
    if (Side->Bad == 0) {
      Side->FirstError = Status;
    }

    Side->Bad++;
  }

  Side->Rounds++;
}

STATIC
VOID
EFIAPI
Tick (
  IN EFI_EVENT  Event,
  IN VOID       *Context
  )
{
  if (mStop) {
    return;
  }

  Round (&mCb);
  if (mCb.Rounds >= VFSRACE_CALLBACKS) {
    mStop = TRUE;
  }
}

EFI_STATUS
EFIAPI
VfsRaceMain (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_STATUS                 Status;
  EFI_LOADED_IMAGE_PROTOCOL  *Image;
  EFI_EVENT                  Timer;

  Status = gBS->HandleProtocol (ImageHandle, &gEfiLoadedImageProtocolGuid, (VOID **)&Image);
  if (!EFI_ERROR (Status)) {
    Status = gBS->HandleProtocol (Image->DeviceHandle, &gEfiSimpleFileSystemProtocolGuid, (VOID **)&mFs);
  }

  if (EFI_ERROR (Status)) {
    Print (L"vfsrace: ERROR no file system: %r\n", Status);
    return Status;
  }

  Status = ReadOnce (&mFg, &mReferenceSize);
  if (EFI_ERROR (Status) || (mReferenceSize == 0)) {
    Print (L"vfsrace: ERROR cannot read %s: %r\n", VFSRACE_FILE, Status);
    return EFI_ERROR (Status) ? Status : EFI_NOT_FOUND;
  }

  CopyMem (mReference, mFg.Buffer, mReferenceSize);
  Print (L"vfsrace: %s is %u bytes; racing %u timer rounds\n", VFSRACE_FILE, (UINT32)mReferenceSize, VFSRACE_CALLBACKS);

  Status = gBS->CreateEvent (EVT_TIMER | EVT_NOTIFY_SIGNAL, TPL_CALLBACK, Tick, NULL, &Timer);
  if (!EFI_ERROR (Status)) {
    Status = gBS->SetTimer (Timer, TimerPeriodic, 10000);
  }

  if (EFI_ERROR (Status)) {
    Print (L"vfsrace: ERROR timer: %r\n", Status);
    return Status;
  }

  while (!mStop) {
    Round (&mFg);
    if ((mFg.Rounds % 2000) == 0) {
      Print (L"vfsrace: fg=%u cb=%u\n", mFg.Rounds, mCb.Rounds);
    }
  }

  gBS->SetTimer (Timer, TimerCancel, 0);
  gBS->CloseEvent (Timer);

  if ((mFg.Bad == 0) && (mCb.Bad == 0)) {
    Print (L"vfsrace: PASS fg=%u cb=%u\n", mFg.Rounds, mCb.Rounds);
    return EFI_SUCCESS;
  }

  Print (
    L"vfsrace: FAIL fg=%u bad=%u (%r) cb=%u bad=%u (%r)\n",
    mFg.Rounds,
    mFg.Bad,
    mFg.FirstError,
    mCb.Rounds,
    mCb.Bad,
    mCb.FirstError
    );
  return EFI_DEVICE_ERROR;
}
