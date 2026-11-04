#include "Compiler.h"
#include "Platform.h"
#include "Board.h"
typedef unsigned char uint8;
VAR(uint8, APP_VAR) g_pins = BOARD_PINS;
FUNC(void, APP_CODE) App_Run(void) { g_pins = (uint8)(g_pins + PLATFORM_ID + 1U); }
