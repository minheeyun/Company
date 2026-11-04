#ifndef COMPILER_H
#define COMPILER_H
#define AUTOMATIC
#define VAR(vartype, memclass) memclass vartype
#define FUNC(rettype, memclass) memclass rettype
#if (defined AUTOMATIC_X)
#error "AUTOMATIC_X already defined"
#endif
#endif
