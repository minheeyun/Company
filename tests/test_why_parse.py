import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import why_parse as wp  # noqa: E402

COMPILER_H = """#ifndef COMPILER_H
#define COMPILER_H
#if defined(_GREENHILLS_C_CYTXXX_)
#define AUTOMATIC
#define VAR(vartype, memclass) vartype
#define P2VAR(ptrtype, memclass, ptrclass) ptrtype *
#endif
#endif
"""


@unittest.skipUnless(shutil.which("gcc"), "gcc 필요")
class WhyParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name)
        files = {
            "Base/GHS/Compiler.h": COMPILER_H,
            "Os/inc/Compiler.h": "/* stub */\n",
            "Os/inc/Os_Types.h": '#include "Compiler.h"\ntypedef unsigned char uint8;\n'
                                 "typedef P2VAR(uint8, AUTOMATIC, OS_VAR) RefType;\n",
            "src/a.c": '#include "Os_Types.h"\nVAR(uint8, X_VAR) g;\n',
        }
        for rel, text in files.items():
            (self.p / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.p / rel).write_text(text, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wp.main([str(self.p), "src/a.c", "-I", "Base/GHS", *extra])
        return buf.getvalue()

    def test_stub_next_to_includer_wins(self):
        out = self.run_tool()
        self.assertIn("Os/inc/Compiler.h", out.replace("\\", "/"))
        self.assertIn("Base/GHS/Compiler.h  정의: AUTOMATIC, P2VAR, VAR  → include 안 됨", out)
        self.assertIn("파싱 실패", out)

    def test_conditional_define_reported_and_fixed(self):
        (self.p / "Os/inc/Compiler.h").unlink()
        out = self.run_tool()
        self.assertIn("→ include 됨", out)
        self.assertIn("#if defined(_GREENHILLS_C_CYTXXX_)", out)
        self.assertIn("파싱 성공", self.run_tool("-D", "_GREENHILLS_C_CYTXXX_"))


@unittest.skipUnless(shutil.which("gcc"), "gcc 필요")
class MemclassTests(unittest.TestCase):
    def test_template_compiler_cfg_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            files = {
                "Mcal/include/GHS/Compiler.h": '#include "Compiler_Cfg.h"\n#define VAR(vartype, memclass) memclass vartype\n',
                "Mcal/templates/Compiler_Cfg.h": "/* template */\n",
                "Yura/Config/Compiler_Cfg.h": "#define DET_VAR_NOINIT\n",
                "Det/Det.h": '#include "Compiler.h"\ntypedef unsigned char uint8;\nextern VAR(uint8, DET_VAR_NOINIT) ErrorCount;\n',
                "src/a.c": '#include "Det.h"\n',
            }
            for rel, text in files.items():
                (p / rel).parent.mkdir(parents=True, exist_ok=True)
                (p / rel).write_text(text, encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                wp.main([str(p), "src/a.c", "-I", "Mcal/include/GHS"])
            out = buf.getvalue()
            self.assertIn("'DET_VAR_NOINIT' 가 정의되지 않았습니다", out)
            self.assertIn("정의 위치: Yura/Config/Compiler_Cfg.h  → include 안 됨", out)
            self.assertIn('--exclude "*templates*"', out)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                wp.main([str(p), "src/a.c", "-I", "Mcal/include/GHS", "-I", "Yura/Config", "--exclude", "*templates*"])
            self.assertIn("파싱 성공", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
