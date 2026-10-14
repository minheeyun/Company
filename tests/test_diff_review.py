import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import diff_review as r  # noqa: E402
import gitlab_diff_export as g  # noqa: E402

SAMPLE = ROOT / "examples" / "autosar_sample_diffs.json"

SAMPLE_REVIEW = """## 1. 변경 배경
- 실차 진단 중 S3 타이머 만료가 10ms 늦게 판정됨

## 2. 리뷰 결과
`Dcm_CheckSessionTimeout()`의 비교가 `>=`로 수정되어, 코드로 확인함.
단, 수정 전후 재현 로그 확인 필요.

## 3. 검증자 메모
| No | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|
| 1 | Extended Session | 진단 요청 중단 5000ms | Default 전환 | DID F186 |

- 회귀: **세션 제어**
"""


class FakeStream:
    def __init__(self, msg, calls, kwargs):
        self.msg, self.calls, self.kwargs = msg, calls, kwargs

    def __enter__(self):
        self.calls.append(self.kwargs)
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, msg):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(msg, self.calls, kw)))


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "export"
        g.export(json.loads(SAMPLE.read_text(encoding="utf-8")), self.out, "side-by-side", "grp/ecu !42")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_from_html(self):
        items = r.load_items([str(self.out)])
        self.assertEqual(len(items), 2)
        self.assertEqual({i["new_path"] for i in items}, {"src/BswM/Dcm_Callout.c", "src/NvM/NvM_Cfg_App.h"})
        self.assertEqual(items[0]["source"], "grp/ecu !42")
        only_c = r.load_items([str(self.out)], include=["*.c"])
        self.assertEqual([i["new_path"] for i in only_c], ["src/BswM/Dcm_Callout.c"])

    def test_html_without_data_is_rejected(self):
        bad = Path(self.tmp.name) / "bad.html"
        bad.write_text("<html><body>no data</body></html>", encoding="utf-8")
        with self.assertRaises(ValueError):
            r.read_diff_html(bad)

    def test_format_diff_has_function_and_line_numbers(self):
        text = r.format_diff(r.load_items([str(self.out)], include=["*.c"]))
        self.assertIn("### 파일: src/BswM/Dcm_Callout.c", text)
        self.assertIn("static void Dcm_CheckSessionTimeout(void)", text)
        self.assertRegex(text, r"\n\s+143\s+- +if \(Dcm_S3Timer > DCM_S3_TIMEOUT_MS\)")
        self.assertRegex(text, r"\n\s+143\s+\+ +if \(Dcm_S3Timer >= DCM_S3_TIMEOUT_MS\)")

    def test_user_content_marks_missing_inputs(self):
        att = [r.read_attachment("결함 티켓 번호", "ECU-1234")]
        content = r.build_user_content("DIFF", att)
        text = content[-1]["text"]
        self.assertIn('<자료 항목="결함 티켓 번호" 출처="(직접 입력)">\nECU-1234', text)
        self.assertIn('<자료 항목="테스트 결과">제공되지 않음</자료>', text)
        self.assertIn("## 3. 검증자 메모", text)

    def test_pdf_attachment_becomes_document_block(self):
        pdf = Path(self.tmp.name) / "spec.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        content = r.build_user_content("DIFF", [r.read_attachment("관련 사양", str(pdf))])
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")

    def test_split_and_render(self):
        record, memo = r.split_review(SAMPLE_REVIEW)
        self.assertIn("## 2. 리뷰 결과", record)
        self.assertNotIn("검증자 메모", record)
        self.assertTrue(memo.startswith("## 3. 검증자 메모"))
        page = r.render_review_html(record, memo, "meta")
        self.assertIn("<th>사전 조건</th>", page)
        self.assertIn("<td>DID F186</td>", page)
        self.assertIn("<code>Dcm_CheckSessionTimeout()</code>", page)
        self.assertIn("<strong>세션 제어</strong>", page)

    def test_run_review_request_shape(self):
        msg = SimpleNamespace(stop_reason="end_turn",
                              content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=SAMPLE_REVIEW)])
        client = FakeClient(msg)
        out = r.run_review("SYS", [{"type": "text", "text": "x"}], client=client)
        self.assertEqual(out, SAMPLE_REVIEW.strip())
        kw = client.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["thinking"], {"type": "adaptive"})
        self.assertEqual(kw["system"][0]["text"], "SYS")
        self.assertEqual(kw["fallbacks"], "default")

    def test_refusal_raises(self):
        client = FakeClient(SimpleNamespace(stop_reason="refusal", content=[]))
        with self.assertRaises(r.ReviewError):
            r.run_review("SYS", [], client=client)

    def test_dry_run_cli(self):
        rc = r.main([str(self.out), "-t", "ECU-1234", "--dry-run"])
        self.assertEqual(rc, 0)
        user = (self.out / "review" / "prompt_user.md").read_text(encoding="utf-8")
        self.assertIn("Dcm_CheckSessionTimeout", user)
        system = (self.out / "review" / "prompt_system.md").read_text(encoding="utf-8")
        self.assertIn("AUTOSAR Classic", system)


if __name__ == "__main__":
    unittest.main()
