import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cpp_filter as cf  # noqa: E402


class CleanTests(unittest.TestCase):
    def test_inline_asm_removed_keeping_lines(self):
        src = 'void f(void) {\n  __asm volatile ("isb 0xF"\n     ::: "memory");\n  x = 1;\n}\n'
        out = cf.clean(src)
        self.assertNotIn("__asm", out)
        self.assertEqual(out.count("\n"), src.count("\n"))
        self.assertIn("x = 1;", out)

    def test_asm_variants_and_strings_with_parens(self):
        out = cf.clean('asm goto ("j %l0" :::: L); __asm__ volatile ("mov r0, \\")\\"" : "=r"(r)); y;')
        self.assertEqual(out.strip(), "; ; y;")

    def test_pragma_removed(self):
        out = cf.clean('struct S {\n#pragma ghs section bss=".x"\n int a; };\n_Pragma("pack()") int b;\n# 1 "a.c"\n')
        self.assertNotIn("pragma", out.lower())
        self.assertIn('# 1 "a.c"', out)
        self.assertEqual(out.count("\n"), 5)

    def test_mixed_encoding_lines(self):
        self.assertEqual(cf._decode_line("한글".encode("cp949")), "한글")
        self.assertEqual(cf._decode_line("한글".encode("utf-8")), "한글")


@unittest.skipUnless(shutil.which("gcc"), "gcc 필요")
class RunTests(unittest.TestCase):
    def test_runs_gcc_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.c"
            p.write_bytes('#define __ASM __asm\nconst char *m = "한글";\nvoid f(void){ __ASM volatile ("nop"); }\n'
                          .encode("cp949"))
            r = subprocess.run([sys.executable, str(ROOT / "cpp_filter.py"), "-E", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("__asm", r.stdout)
            self.assertIn("한글", r.stdout)

    def test_passes_gcc_errors_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.c"
            p.write_text('#include "nope.h"\n')
            r = subprocess.run([sys.executable, str(ROOT / "cpp_filter.py"), str(p)], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("nope.h", r.stderr)


if __name__ == "__main__":
    unittest.main()
