/* vfia 대체 내장 헤더: 시스템에 Clang 내장 헤더(resource-dir)가 없을 때만 사용한다.
   Clang이 미리 정의하는 __*_TYPE__/__*_MAX__ 매크로로 target에 맞는 정의를 만든다. */
#ifndef __VFIA_ISO646_H
#define __VFIA_ISO646_H
#ifndef __cplusplus
#define and &&
#define and_eq &=
#define bitand &
#define bitor |
#define compl ~
#define not !
#define not_eq !=
#define or ||
#define or_eq |=
#define xor ^
#define xor_eq ^=
#endif
#endif
