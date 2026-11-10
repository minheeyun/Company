/* vfia 대체 내장 헤더: 시스템에 Clang 내장 헤더(resource-dir)가 없을 때만 사용한다.
   Clang이 미리 정의하는 __*_TYPE__/__*_MAX__ 매크로로 target에 맞는 정의를 만든다. */
#ifndef __VFIA_STDBOOL_H
#define __VFIA_STDBOOL_H
#ifndef __cplusplus
#if !defined(__STDC_VERSION__) || __STDC_VERSION__ < 202311L
#define bool _Bool
#define true 1
#define false 0
#endif
#endif
#define __bool_true_false_are_defined 1
#endif
