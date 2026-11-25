/*
 * The types and constants TerminalDxe's IsKeyRegistered uses, as
 * MdePkg/Include/Protocol/SimpleTextIn.h and SimpleTextInEx.h define them
 * at the pinned tag, so that the function can be compiled on the host.
 */
#ifndef TERMINAL_KEYS_STUB_H
#define TERMINAL_KEYS_STUB_H

#include <assert.h>
#include <stdint.h>

#define IN
#define TRUE   1
#define FALSE  0
#define ASSERT(x)  assert (x)

typedef unsigned char  BOOLEAN;
typedef uint8_t        UINT8;
typedef uint16_t       UINT16;
typedef uint32_t       UINT32;
typedef uint16_t       CHAR16;

typedef struct {
  UINT16    ScanCode;
  CHAR16    UnicodeChar;
} EFI_INPUT_KEY;

typedef struct {
  UINT32    KeyShiftState;
  UINT8     KeyToggleState;
} EFI_KEY_STATE;

typedef struct {
  EFI_INPUT_KEY    Key;
  EFI_KEY_STATE    KeyState;
} EFI_KEY_DATA;

#define CHAR_BACKSPACE        0x0008
#define CHAR_TAB              0x0009
#define CHAR_LINEFEED         0x000A
#define CHAR_CARRIAGE_RETURN  0x000D

#define EFI_SHIFT_STATE_VALID      0x80000000
#define EFI_RIGHT_SHIFT_PRESSED    0x00000001
#define EFI_LEFT_SHIFT_PRESSED     0x00000002
#define EFI_RIGHT_CONTROL_PRESSED  0x00000004
#define EFI_LEFT_CONTROL_PRESSED   0x00000008
#define EFI_RIGHT_ALT_PRESSED      0x00000010
#define EFI_LEFT_ALT_PRESSED       0x00000020

#endif
