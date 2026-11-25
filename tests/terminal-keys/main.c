/*
 * A host harness for TerminalDxe's IsKeyRegistered, as patched by
 * patches/0004-terminal-control-keys.patch. tests/firmware-tests.sh cuts the
 * function out of the patched TerminalConIn.c into IsKeyRegistered.c and
 * compiles it with this file and stub.h.
 *
 * It prints the characters 0x00-0x7F that a key read from a terminal (no
 * shift state) delivers to a registration of the same character with
 * Control held (left, right, both), then the cases #12281 decided, each
 * as "<case>: <0|1>".
 */
#include <stdio.h>

#include "stub.h"

#include "IsKeyRegistered.c"

static BOOLEAN
Matches (
  UINT16  Char,
  UINT32  RegisteredShift,
  UINT32  InputShift,
  UINT8   RegisteredToggle,
  UINT8   InputToggle
  )
{
  EFI_KEY_DATA  Registered = { { 0, Char }, { RegisteredShift, RegisteredToggle } };
  EFI_KEY_DATA  Input      = { { 0, Char }, { InputShift, InputToggle } };

  return IsKeyRegistered (&Registered, &Input);
}

static void
PrintSet (
  const char  *Name,
  UINT32      RegisteredShift
  )
{
  UINT16  Char;

  printf ("%s:", Name);
  for (Char = 0; Char < 0x80; Char++) {
    if (Matches (Char, RegisteredShift, 0, 0, 0)) {
      printf (" %02x", Char);
    }
  }

  printf ("\n");
}

int
main (
  void
  )
{
  const UINT32  Valid = EFI_SHIFT_STATE_VALID;
  const UINT32  LCtrl = EFI_LEFT_CONTROL_PRESSED;
  const UINT32  RCtrl = EFI_RIGHT_CONTROL_PRESSED;

  PrintSet ("lctrl", Valid | LCtrl);
  PrintSet ("rctrl", Valid | RCtrl);
  PrintSet ("both", LCtrl | RCtrl);
  PrintSet ("lctrl+lshift", Valid | LCtrl | EFI_LEFT_SHIFT_PRESSED);
  PrintSet ("lalt", Valid | EFI_LEFT_ALT_PRESSED);
  printf ("rctrl n, plain n: %d\n", Matches ('n', Valid | RCtrl, Valid, 0, 0));
  printf ("rctrl n, rctrl n: %d\n", Matches ('n', Valid | RCtrl, Valid | RCtrl, 0, 0));
  printf ("plain 03, plain 03: %d\n", Matches (0x03, 0, 0, 0, 0));
  printf ("plain 0d, plain 0d: %d\n", Matches (0x0D, Valid, 0, 0, 0));
  printf ("lctrl 03, valid-only 03: %d\n", Matches (0x03, Valid | LCtrl, Valid, 0, 0));
  printf ("lctrl 0d, valid-only 0d: %d\n", Matches (0x0D, Valid | LCtrl, Valid, 0, 0));
  printf ("lctrl 03, plain 03, toggle differs: %d\n", Matches (0x03, Valid | LCtrl, 0, 0x40, 0));
  printf ("lctrl 03, lshift 03: %d\n", Matches (0x03, Valid | LCtrl, Valid | EFI_LEFT_SHIFT_PRESSED, 0, 0));
  return 0;
}
