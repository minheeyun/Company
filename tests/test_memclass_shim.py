import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import memclass_shim as ms  # noqa: E402


class ShimTests(unittest.TestCase):
    def test_collects_memclass_positions_and_skips_compiler_defined(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "inc").mkdir()
            (p / "inc/Compiler.h").write_text(
                "#if (defined AUTOMATIC) /* to prevent double definition */\n#error AUTOMATIC already defined\n#endif\n"
                "#define AUTOMATIC\n#define VAR(vartype, memclass) memclass vartype\n"
                "#define P2VAR(ptrtype, memclass, ptrclass) ptrclass ptrtype * memclass\n")
            (p / "tmpl").mkdir()
            (p / "tmpl/Compiler_Cfg.h").write_text("#define OS_VAR\n")  # 쓰이지 않는 템플릿에만 정의된 이름
            (p / "a.c").write_text(
                "/* VAR(uint8, IN_COMMENT) */\n"
                "VAR(uint8, DET_VAR_NOINIT) x;\n"
                "typedef P2VAR(EventMaskType, AUTOMATIC, OS_VAR) Ref;\n"
                "typedef P2FUNC(void, OS_APPL_CODE, CbType)(uint8 v);\n"
                "FUNC(P2VAR(uint8, AUTOMATIC, NESTED_PTR), MY_CODE) f(void);\n"
                "CONSTP2FUNC(void, CB_CODE, k)(void);\n")
            names, compiler_defined = ms.collect([p], [])
            self.assertIn("AUTOMATIC", compiler_defined)
            for n in ("DET_VAR_NOINIT", "OS_VAR", "OS_APPL_CODE", "MY_CODE", "CB_CODE"):
                self.assertIn(n, names)
            self.assertNotIn("IN_COMMENT", names)
            self.assertNotIn("CbType", names)  # 함수 포인터 이름 자리는 제외

            out = p / "shim.h"
            self.assertEqual(ms.main([str(p), "-o", str(out)]), 0)
            text = out.read_text()
            self.assertIn("#ifndef DET_VAR_NOINIT\n#define DET_VAR_NOINIT\n#endif", text)
            self.assertNotIn("define AUTOMATIC", text)
            self.assertIn("#define OS_VAR", text)  # 다른 Compiler*.h 에 있어도 #error 검사 대상이 아니면 정의

    def test_split_args_nested(self):
        args, _ = ms.split_args("a, P2VAR(x, y, z), c) rest", 0)
        self.assertEqual(args, ["a", "P2VAR(x, y, z)", "c"])


if __name__ == "__main__":
    unittest.main()
