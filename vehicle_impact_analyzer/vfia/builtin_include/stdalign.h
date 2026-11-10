/* vfia 대체 내장 헤더: 시스템에 Clang 내장 헤더(resource-dir)가 없을 때만 사용한다.
   Clang이 미리 정의하는 __*_TYPE__/__*_MAX__ 매크로로 target에 맞는 정의를 만든다. */
#ifndef __VFIA_STDALIGN_H
#define __VFIA_STDALIGN_H
#ifndef __cplusplus
#define alignas _Alignas
#define alignof _Alignof
#endif
#define __alignas_is_defined 1
#define __alignof_is_defined 1
#endif
