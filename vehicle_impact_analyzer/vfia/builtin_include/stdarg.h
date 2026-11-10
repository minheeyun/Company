/* vfia 대체 내장 헤더: 시스템에 Clang 내장 헤더(resource-dir)가 없을 때만 사용한다.
   Clang이 미리 정의하는 __*_TYPE__/__*_MAX__ 매크로로 target에 맞는 정의를 만든다. */
#ifndef __VFIA_STDARG_H
#define __VFIA_STDARG_H
typedef __builtin_va_list va_list;
typedef __builtin_va_list __gnuc_va_list;
#define va_start(ap, param) __builtin_va_start(ap, param)
#define va_end(ap) __builtin_va_end(ap)
#define va_arg(ap, type) __builtin_va_arg(ap, type)
#define va_copy(dest, src) __builtin_va_copy(dest, src)
#endif
