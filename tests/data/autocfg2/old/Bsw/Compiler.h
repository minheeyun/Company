#ifndef COMPILER_H
#define COMPILER_H
#if defined(__ghs__)
#define INLINE __inline
#elif defined(__TASKING__)
#define INLINE inline
#else
#error "Unknown compiler"
#endif
#define FUNC(rt, mc) rt
#define VAR(t, mc) t
#endif
