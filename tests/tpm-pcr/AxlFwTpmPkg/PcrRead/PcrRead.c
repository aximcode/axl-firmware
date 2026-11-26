/** @file
  The TPM boot check (lib/axlfw/tpm.py): read PCRs 0-7 of the SHA-256 bank
  with TPM2_PCR_Read, submitted through EFI_TCG2_PROTOCOL, and print them.

  Each PCR is a line "axlfw-pcr: sha256 PCR<n> <64 hex digits>", and the
  last line is "axlfw-pcr: done". Anything that stops it (no Tcg2 protocol,
  SubmitCommand failing, a TPM response code, a response that does not
  parse) is a line "axlfw-pcr: error ..." instead, and no "done".
**/

#include <Uefi.h>
#include <Protocol/Tcg2Protocol.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>

#define PCR_COUNT        8
#define RESPONSE_SIZE    512
// The response header: tag (2), size (4), response code (4).
#define RESPONSE_HEADER  10

STATIC
UINT32
Be (
  IN CONST UINT8  *Bytes,
  IN UINTN        Size
  )
{
  UINT32  Value;
  UINTN   Index;

  Value = 0;
  for (Index = 0; Index < Size; Index++) {
    Value = (Value << 8) | Bytes[Index];
  }

  return Value;
}

EFI_STATUS
EFIAPI
UefiMain (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  //
  // TPM2_PCR_Read: TPM_ST_NO_SESSIONS, 20 bytes, TPM_CC_PCR_Read (0x17E);
  // one TPMS_PCR_SELECTION: SHA-256, a 3-byte select of PCRs 0-7.
  //
  STATIC CONST UINT8  Command[] = {
    0x80, 0x01, 0x00, 0x00, 0x00, 0x14, 0x00, 0x00, 0x01, 0x7E,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x0B, 0x03, 0xFF, 0x00, 0x00
  };
  UINT8               Response[RESPONSE_SIZE];
  EFI_TCG2_PROTOCOL   *Tcg2;
  EFI_STATUS          Status;
  UINT32              Size;
  UINT32              Code;
  UINT32              Count;
  UINT32              Offset;
  UINT32              Digest;
  UINT32              Byte;

  Status = gBS->LocateProtocol (&gEfiTcg2ProtocolGuid, NULL, (VOID **)&Tcg2);
  if (EFI_ERROR (Status)) {
    Print (L"axlfw-pcr: error: no Tcg2 protocol: %r\n", Status);
    return Status;
  }

  Status = Tcg2->SubmitCommand (Tcg2, sizeof Command, (UINT8 *)Command, sizeof Response, Response);
  if (EFI_ERROR (Status)) {
    Print (L"axlfw-pcr: error: SubmitCommand: %r\n", Status);
    return Status;
  }

  Size = Be (Response + 2, 4);
  Code = Be (Response + 6, 4);
  if ((Size < RESPONSE_HEADER) || (Size > sizeof Response)) {
    Print (L"axlfw-pcr: error: a response of %u bytes\n", Size);
    return EFI_PROTOCOL_ERROR;
  }

  if (Code != 0) {
    Print (L"axlfw-pcr: error: response code 0x%x\n", Code);
    return EFI_DEVICE_ERROR;
  }

  //
  // pcrUpdateCounter (4); pcrSelectionOut: exactly the selection asked for;
  // pcrValues: PCR_COUNT digests of SHA256_DIGEST_SIZE bytes, each after its size.
  //
  Offset = RESPONSE_HEADER + 4;
  if ((Size < Offset + 10) || (Be (Response + Offset, 4) != 1) ||
      (Be (Response + Offset + 4, 2) != TPM_ALG_SHA256) || (Response[Offset + 6] != 3) ||
      (Response[Offset + 7] != 0xFF) || (Response[Offset + 8] != 0) || (Response[Offset + 9] != 0))
  {
    Print (L"axlfw-pcr: error: the TPM did not select sha256 PCRs 0-7\n");
    return EFI_PROTOCOL_ERROR;
  }

  Offset += 10;
  Count   = (Size >= Offset + 4) ? Be (Response + Offset, 4) : 0;
  Offset += 4;
  if ((Count != PCR_COUNT) || (Size != Offset + PCR_COUNT * (2 + SHA256_DIGEST_SIZE))) {
    Print (L"axlfw-pcr: error: %u digests in a response of %u bytes\n", Count, Size);
    return EFI_PROTOCOL_ERROR;
  }

  for (Digest = 0; Digest < PCR_COUNT; Digest++) {
    if (Be (Response + Offset, 2) != SHA256_DIGEST_SIZE) {
      Print (L"axlfw-pcr: error: PCR%u's digest is not %u bytes\n", Digest, SHA256_DIGEST_SIZE);
      return EFI_PROTOCOL_ERROR;
    }

    Offset += 2;
    Print (L"axlfw-pcr: sha256 PCR%u ", Digest);
    for (Byte = 0; Byte < SHA256_DIGEST_SIZE; Byte++) {
      Print (L"%02x", Response[Offset + Byte]);
    }

    Print (L"\n");
    Offset += SHA256_DIGEST_SIZE;
  }

  Print (L"axlfw-pcr: done\n");
  return EFI_SUCCESS;
}
