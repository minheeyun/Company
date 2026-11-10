/* vfia 대체 내장 헤더: 시스템에 Clang 내장 헤더(resource-dir)가 없을 때만 사용한다.
   Clang이 미리 정의하는 __*_TYPE__/__*_MAX__ 매크로로 target에 맞는 정의를 만든다. */
#ifndef __VFIA_STDDEF_H
#define __VFIA_STDDEF_H
#if !defined(_SIZE_T_DEFINED) && !defined(_SIZE_T)
#define _SIZE_T_DEFINED
#define _SIZE_T
typedef __SIZE_TYPE__ size_t;
#endif
#if !defined(_PTRDIFF_T_DEFINED) && !defined(_PTRDIFF_T)
#define _PTRDIFF_T_DEFINED
#define _PTRDIFF_T
typedef __PTRDIFF_TYPE__ ptrdiff_t;
#endif
#if !defined(__cplusplus) && !defined(_WCHAR_T_DEFINED) && !defined(_WCHAR_T)
#define _WCHAR_T_DEFINED
#define _WCHAR_T
typedef __WCHAR_TYPE__ wchar_t;
#endif
#ifndef NULL
#define NULL ((void *)0)
#endif
#ifndef offsetof
#define offsetof(t, m) __builtin_offsetof(t, m)
#endif
#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
typedef struct { long long __vfia_ll; long double __vfia_ld; } max_align_t;
#endif
#endif
