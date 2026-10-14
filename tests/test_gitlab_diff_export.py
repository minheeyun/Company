import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gitlab_diff_export as g  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_diffs.json"


class ParseTests(unittest.TestCase):
    def test_line_numbers_and_kinds(self):
        hunks = g.parse_hunks("@@ -10,3 +10,3 @@ ctx\n a\n-b\n+c\n d\n")
        self.assertEqual(len(hunks), 1)
        lines = hunks[0].lines
        self.assertEqual([l.kind for l in lines], ["ctx", "del", "add", "ctx"])
        self.assertEqual((lines[0].old_no, lines[0].new_no), (10, 10))
        self.assertEqual((lines[1].old_no, lines[1].new_no), (11, None))
        self.assertEqual((lines[2].old_no, lines[2].new_no), (None, 11))
        self.assertEqual((lines[3].old_no, lines[3].new_no), (12, 12))

    def test_inline_highlight(self):
        lines = g.parse_hunks("@@ -1 +1 @@\n-return 0\n+return 1\n")[0].lines
        self.assertIn(("0", True), lines[0].inline)
        self.assertIn(("1", True), lines[1].inline)

    def test_no_newline_marker(self):
        lines = g.parse_hunks("@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n")[0].lines
        self.assertEqual(lines[-1].kind, "meta")

    def test_ignores_git_headers(self):
        hunks = g.parse_hunks("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual([l.kind for l in hunks[0].lines], ["del", "add"])

    def test_status(self):
        items = json.loads(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual([g.to_file_diff(i).status for i in items],
                         ["modified", "added", "renamed", "deleted", "modified"])
        self.assertIsNotNone(g.to_file_diff(items[4]).note)  # binary


class OutputTests(unittest.TestCase):
    def test_safe_rel_path(self):
        self.assertEqual(g.safe_rel_path("../../etc/passwd").as_posix(), "etc/passwd.html")
        self.assertEqual(g.safe_rel_path("a/b:c.txt").as_posix(), "a/b_c.txt.html")

    def test_export_writes_one_html_per_file(self):
        items = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for view in ("unified", "side-by-side"):
            with tempfile.TemporaryDirectory() as d:
                out = Path(d)
                written = g.export(items, out, view, "sample")
                self.assertEqual(len(written), len(items))
                self.assertTrue((out / "files/src/app.py.html").exists())
                self.assertTrue((out / "files/new/name.txt.html").exists())
                page = (out / "files/docs/README.md.html").read_text(encoding="utf-8")
                self.assertIn("&lt;b&gt;설명&lt;/b&gt; &amp; 소개", page)  # HTML escaping
                self.assertIn('href="../../index.html"', page)
                index = (out / "index.html").read_text(encoding="utf-8")
                self.assertIn('href="files/src/app.py.html"', index)

    def test_case_collision(self):
        items = [{"old_path": p, "new_path": p, "diff": "@@ -1 +1 @@\n-a\n+b\n"} for p in ("A.txt", "a.txt")]
        with tempfile.TemporaryDirectory() as d:
            written = g.export(items, Path(d))
            self.assertEqual(len({w.name.lower() for w in written}), 2)


class ApiTests(unittest.TestCase):
    def test_parse_mr_url(self):
        self.assertEqual(g.parse_mr_url("https://gitlab.example.com/grp/sub/proj/-/merge_requests/12/diffs"),
                         ("https://gitlab.example.com", "grp/sub/proj", 12))
        with self.assertRaises(ValueError):
            g.parse_mr_url("https://gitlab.example.com/grp/proj")

    def test_pagination_and_auth(self):
        pages = {"1": ([{"new_path": "a"}], "2"), "2": ([{"new_path": "b"}], "")}
        seen = []

        class Resp(io.BytesIO):
            def __init__(self, body, nxt):
                super().__init__(json.dumps(body).encode())
                self.headers = {"X-Next-Page": nxt}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen.append(req)
            page = req.full_url.split("page=")[-1]
            return Resp(*pages[page])

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            items = g.GitLabClient("https://gl.example", "tok").mr_diffs("grp/proj", 7)
        self.assertEqual([i["new_path"] for i in items], ["a", "b"])
        self.assertIn("/api/v4/projects/grp%2Fproj/merge_requests/7/diffs", seen[0].full_url)
        self.assertEqual(seen[0].get_header("Private-token"), "tok")


if __name__ == "__main__":
    unittest.main()
