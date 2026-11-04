#if defined(CPU_BOARD_REVA)
#define BOARD_PINS 4
#elif defined(CPU_BOARD_REVC)
#define BOARD_PINS 8
#else
#error "Undefined Revision"
#endif
