/** @file
  TcpHold: a resident TCP4 server that accepts one connection on port 5555
  and holds it open, with a Receive pending, as a resident service does.

  Loaded with the Shell's `load`, after the NIC has an address (`ifconfig -s
  eth0 static ...`). With a client connected, a `reconnect -r` (or a
  disconnect of the NIC) must return, destroying both the listener and the
  accepted connection; their tokens complete with an error.

  Prints:
    tcphold: listening on port 5555
    tcphold: accepted
    tcphold: received <n> bytes
    tcphold: receive ended: <status>   (the connection destroyed or closed)
    tcphold: accept ended: <status>    (the listener destroyed)
**/

#include <Uefi.h>
#include <Library/BaseMemoryLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>
#include <Protocol/ServiceBinding.h>
#include <Protocol/Tcp4.h>

#define TCPHOLD_PORT  5555

STATIC EFI_TCP4_PROTOCOL       *mListener;
STATIC EFI_TCP4_PROTOCOL       *mConn;
STATIC EFI_TCP4_LISTEN_TOKEN   mListenToken;
STATIC EFI_TCP4_IO_TOKEN       mRxToken;
STATIC EFI_TCP4_RECEIVE_DATA   mRxData;
STATIC UINT8                   mRxBuffer[512];

STATIC
EFI_STATUS
PostReceive (
  VOID
  )
{
  mRxData.UrgentFlag                  = FALSE;
  mRxData.DataLength                  = sizeof mRxBuffer;
  mRxData.FragmentCount               = 1;
  mRxData.FragmentTable[0].FragmentLength = sizeof mRxBuffer;
  mRxData.FragmentTable[0].FragmentBuffer = mRxBuffer;
  mRxToken.Packet.RxData              = &mRxData;
  return mConn->Receive (mConn, &mRxToken);
}

STATIC
VOID
EFIAPI
OnReceive (
  IN EFI_EVENT  Event,
  IN VOID       *Context
  )
{
  EFI_STATUS  Status;

  Status = mRxToken.CompletionToken.Status;
  if (EFI_ERROR (Status)) {
    Print (L"tcphold: receive ended: %r\n", Status);
    return;
  }

  Print (L"tcphold: received %u bytes\n", mRxData.DataLength);
  Status = PostReceive ();
  if (EFI_ERROR (Status)) {
    Print (L"tcphold: receive ended: %r\n", Status);
  }
}

STATIC
VOID
EFIAPI
OnAccept (
  IN EFI_EVENT  Event,
  IN VOID       *Context
  )
{
  EFI_STATUS  Status;

  Status = mListenToken.CompletionToken.Status;
  if (EFI_ERROR (Status)) {
    Print (L"tcphold: accept ended: %r\n", Status);
    return;
  }

  Status = gBS->HandleProtocol (mListenToken.NewChildHandle, &gEfiTcp4ProtocolGuid, (VOID **)&mConn);
  if (!EFI_ERROR (Status)) {
    Status = gBS->CreateEvent (EVT_NOTIFY_SIGNAL, TPL_CALLBACK, OnReceive, NULL, &mRxToken.CompletionToken.Event);
  }

  if (!EFI_ERROR (Status)) {
    Status = PostReceive ();
  }

  if (EFI_ERROR (Status)) {
    Print (L"tcphold: accepted, but receive: %r\n", Status);
  } else {
    Print (L"tcphold: accepted\n");
  }
}

EFI_STATUS
EFIAPI
TcpHoldMain (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_STATUS                    Status;
  EFI_HANDLE                    *Handles;
  UINTN                         Count;
  EFI_SERVICE_BINDING_PROTOCOL  *Sb;
  EFI_HANDLE                    Child;
  EFI_TCP4_CONFIG_DATA          Config;

  Status = gBS->LocateHandleBuffer (ByProtocol, &gEfiTcp4ServiceBindingProtocolGuid, NULL, &Count, &Handles);
  if (EFI_ERROR (Status)) {
    Print (L"tcphold: ERROR no TCP4 service: %r\n", Status);
    return Status;
  }

  Status = gBS->HandleProtocol (Handles[0], &gEfiTcp4ServiceBindingProtocolGuid, (VOID **)&Sb);
  gBS->FreePool (Handles);
  Child = NULL;
  if (!EFI_ERROR (Status)) {
    Status = Sb->CreateChild (Sb, &Child);
  }

  if (!EFI_ERROR (Status)) {
    Status = gBS->HandleProtocol (Child, &gEfiTcp4ProtocolGuid, (VOID **)&mListener);
  }

  if (EFI_ERROR (Status)) {
    Print (L"tcphold: ERROR TCP4 child: %r\n", Status);
    return Status;
  }

  ZeroMem (&Config, sizeof Config);
  Config.TimeToLive                    = 64;
  Config.AccessPoint.UseDefaultAddress = TRUE;
  Config.AccessPoint.StationPort       = TCPHOLD_PORT;
  Config.AccessPoint.ActiveFlag        = FALSE;
  Status                               = mListener->Configure (mListener, &Config);
  if (!EFI_ERROR (Status)) {
    Status = gBS->CreateEvent (EVT_NOTIFY_SIGNAL, TPL_CALLBACK, OnAccept, NULL, &mListenToken.CompletionToken.Event);
  }

  if (!EFI_ERROR (Status)) {
    Status = mListener->Accept (mListener, &mListenToken);
  }

  if (EFI_ERROR (Status)) {
    Print (L"tcphold: ERROR listen: %r\n", Status);
    Sb->DestroyChild (Sb, Child);
    return Status;
  }

  Print (L"tcphold: listening on port %u\n", TCPHOLD_PORT);
  return EFI_SUCCESS;
}
