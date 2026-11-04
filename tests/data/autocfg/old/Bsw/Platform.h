#if defined(__ghs__)
#define PLATFORM_ID 1
#elif defined(__TASKING__)
#define PLATFORM_ID 2
#else
#error "Compiler is not defined"
#endif
