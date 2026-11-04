"""요약 배치(scripts/summarize_papers.py) 테스트. 가짜 LLM 클라이언트를 쓰고, 실 DB가 떠 있어야 한다.

실행 (backend/ 에서):  python -m unittest tests.test_summarize_papers

OpenAI를 부르지 않는다. 임시 논문(source='test-summarize')을 넣고 끝나면 지운다
(paper_analysis는 ON DELETE CASCADE로 같이 지워진다). 007 마이그레이션이 적용돼 있어야 한다.
"""
import json
import unittest
from unittest import mock
from types import SimpleNamespace
from uuid import uuid4

from app.db import functions
from app.db.connection import get_connection
from app.schemas import AnalysisIn, PaperIn
from scripts import summarize_papers
from scripts.summarize_papers import BANNED_WORDS, MAX_RETRIES, Drug, Summary, check, run

SOURCE = "test-summarize"
GOOD = {
    "title_ko": "치매 환자의 수면 문제와 보호자 부담",
    "summary_background": "배경",
    "summary_finding": "발견",
    "summary_limitation": "한계",
}


class FakeClient:
    """openai.OpenAI 자리에 끼우는 가짜. 정해 둔 문자열을 그대로 돌려주고 호출 횟수를 센다.

    run(polish=True)이면 논문 한 편에 '처음 쓰기' + '다듬기' 두 번 부른다. 다듬기 호출에는 polish를 돌려주고,
    polish를 안 줬으면 처음 쓰기 내용에 빈 changes만 붙여 돌려준다 (= 아무것도 안 바꾼 다듬기).
    """

    def __init__(self, content: str, finish_reason: str = "stop", then: str | None = None,
                 polish: str | None = None):
        self.content = content
        self.then = then  # 두 번째 '처음 쓰기'부터 돌려줄 내용 (없으면 계속 content)
        self.polish = polish
        self.drafts = 0
        self.finish_reason = finish_reason
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if kwargs["response_format"]["json_schema"]["name"] == "paper_polish":
            content = self.polish or json.dumps({**json.loads(self.content), "changes": []})
        else:
            self.drafts += 1
            if self.drafts == 2 and self.then is not None:
                self.content = self.then
            content = self.content
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=self.finish_reason
            )],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


def _delete_test_papers() -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM papers WHERE source = %s", (SOURCE,))


class SummarizePapersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            _delete_test_papers()
        except Exception as exc:
            raise unittest.SkipTest(f"DB에 접속할 수 없습니다 — docker compose up -d ({type(exc).__name__})")

    def setUp(self):
        self.addCleanup(_delete_test_papers)
        result = functions.save_papers([
            PaperIn(source=SOURCE, external_id=str(uuid4()), title="With abstract", abstract="BACKGROUND: ..."),
            PaperIn(source=SOURCE, external_id=str(uuid4()), title="No abstract"),
        ])
        details = {p.title: p for p in functions.get_papers_by_ids(result.inserted_ids)}
        self.paper_id = details["With abstract"].paper_id
        self.papers = [(p.paper_id, p.title, p.abstract) for p in details.values()]

    def test_saves_summary_and_skips_paper_without_abstract(self):
        client = FakeClient(json.dumps(GOOD))
        report = run(self.papers, client, "fake-model")

        self.assertEqual((len(report.done), report.no_abstract, report.failed), (1, 1, []))
        self.assertEqual(client.calls, 1)  # 다듬기는 기본으로 끈다. 초록 없는 논문은 LLM을 부르지 않는다
        self.assertEqual((report.prompt_tokens, report.completion_tokens), (100, 50))
        self.assertIsNone(report.done[0][5])
        self.assertNotIn("다듬기에서 바뀐 표현", summarize_papers.to_markdown(report, "fake-model"))
        saved = functions.get_paper_detail(self.paper_id)
        self.assertEqual(saved.title_ko, GOOD["title_ko"])
        self.assertEqual(saved.summary_background, "배경")
        self.assertEqual(saved.summary_finding, "발견")
        self.assertEqual(saved.summary_limitation, "한계")

    def test_invalid_response_is_not_saved_and_reported_as_failed(self):
        bad_clients = [
            FakeClient("JSON이 아님"),
            FakeClient(json.dumps({**GOOD, "summary_limitation": ""})),
            FakeClient(json.dumps({**GOOD, "summary_finding": "   "})),      # 공백뿐인 칸
            FakeClient(json.dumps({**GOOD, "title_ko": "\n\t "})),
            FakeClient(json.dumps({"title_ko": "칸 부족"})),
            FakeClient(json.dumps(GOOD), finish_reason="length"),  # 토큰 한도에서 잘린 응답
        ]
        for client in bad_clients:
            with self.subTest(content=client.content, finish_reason=client.finish_reason):
                report = run(self.papers, client, "fake-model")

                self.assertEqual(report.done, [])
                self.assertEqual([paper_id for _, paper_id, _ in report.failed], [self.paper_id])
                self.assertEqual(client.calls, MAX_RETRIES + 1)
                self.assertIsNone(functions.get_paper_detail(self.paper_id).summary_finding)

    def test_check_flags_stats_english_and_broken_words(self):
        def problems(**changed):
            return check(Summary(**{**GOOD, **changed}))

        self.assertEqual(problems(), [])
        self.assertEqual(problems(summary_finding="아우벨리티(Auvelity)를 쓴 사람 31명을 6개월 동안 살폈습니다."), [])
        for field, text in [
            ("summary_finding", "차이가 있었습니다(p < 0.05)."),
            ("summary_finding", "신뢰구간(95% CI)을 구했습니다."),
            ("summary_finding", "위험이 높았습니다(OR = 2.1)."),
            ("summary_finding", "β가 컸습니다."),
            ("summary_limitation", "보호자 부담을 ZBI 설문으로 쟀습니다."),
            ("summary_background", "덱스트로methorphan을 썼습니다."),
            ("title_ko", "아우벨리티(Auvelity)의 효과"),  # 제목은 괄호 안 영어도 안 된다
        ]:
            with self.subTest(text=text):
                self.assertTrue(problems(**{field: text}))

    def test_check_allows_units_and_english_in_parentheses_after_drug_name(self):
        def problems(text):
            return check(Summary(**{**GOOD, "summary_finding": text}))

        for ok in [
            "하루 10mg을 12주 동안 먹었습니다.",
            "2.5 mL씩, 몸무게 1kg당 5mg/kg을 썼습니다.",
            "용량은 mg 단위로 적었습니다.",
            "아우벨리티(dextromethorphan-bupropion)를 썼습니다.",
            "두 약(donepezil, memantine)을 함께 썼습니다.",
            "나멘다(Namenda XR)를 썼습니다.",
            "새 약 DSP-2342를 쥐에게 썼습니다.",   # 약·물질 코드명 (대문자 + 숫자)
            "MW073은 공격적인 행동을 줄였습니다.",
        ]:
            with self.subTest(ok=ok):
                self.assertEqual(problems(ok), [])
        for bad in ["donepezil 10mg을 먹었습니다.", "MMSE 점수가 올랐습니다.", "(Auvelity)를 썼습니다.",
                    "DSP 계열 약을 썼습니다."]:  # 숫자가 없는 약자는 코드명이 아니다
            with self.subTest(bad=bad):
                self.assertTrue(problems(bad))
        # 제목은 괄호 안 영어도 여전히 안 된다.
        self.assertTrue(check(Summary(**{**GOOD, "title_ko": "아우벨리티(Auvelity)의 효과"})))

    def test_blank_summary_cells_are_rejected_by_analysis_in_and_save_summary(self):
        for value in ("", "   ", "\n"):
            for name in ("title_ko", "summary_background", "summary_finding", "summary_limitation"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        AnalysisIn(paper_id=self.paper_id, **{name: value})
        analysis = AnalysisIn(paper_id=self.paper_id, **GOOD)
        analysis.summary_finding = "  "  # 만든 뒤에 바꾼 값도 저장 직전에 걸린다
        with self.assertRaises(ValueError):
            functions.save_summary(analysis)
        self.assertIsNone(functions.get_paper_detail(self.paper_id).summary_finding)

    def test_whitespace_only_row_is_returned_by_get_new_papers_again(self):
        def pending():
            return [p.id for p in functions.get_new_papers(limit=10, source=SOURCE)]

        functions.save_summary(AnalysisIn(paper_id=self.paper_id, **GOOD))
        self.assertNotIn(self.paper_id, pending())
        # 예전 버그로 공백만 들어간 행 (지금은 save_summary가 막으므로 직접 만든다).
        with get_connection() as conn:
            conn.execute("UPDATE paper_analysis SET summary_finding = '  ' WHERE paper_id = %s", (self.paper_id,))
        self.assertIn(self.paper_id, pending())

    def test_check_flags_banned_words_in_title_and_body(self):
        for word in BANNED_WORDS:
            for field in ("title_ko", "summary_finding"):
                with self.subTest(word=word, field=field):
                    problems = check(Summary(**{**GOOD, field: f"환자의 {word} 문제를 살폈습니다."}))
                    self.assertTrue(any("금지 단어" in p and word in p for p in problems), problems)
        # 붙여 써도 걸린다.
        self.assertTrue(check(Summary(**{**GOOD, "summary_finding": "인지기능이 떨어졌습니다."})))

    def test_check_flags_drug_name_without_english_on_first_mention(self):
        source = "Auvelity (Dextromethorphan-Bupropion) for Agitation in Alzheimer's Disease"
        drugs = [Drug(ko="아우벨리티", en="Auvelity")]

        def problems(background, finding, drugs=drugs):
            summary = Summary(**{**GOOD, "summary_background": background, "summary_finding": finding})
            return check(summary, drugs, source)

        self.assertEqual(problems("아우벨리티(Auvelity)가 나왔습니다.", "아우벨리티는 증상을 줄였습니다."), [])
        self.assertEqual(problems("배경", "증상이 줄었습니다."), [])  # 본문에 약 이름이 없으면 검사하지 않는다
        # 처음 나오는 곳(①)에 괄호가 없으면 뒤(②)에 있어도 걸린다.
        self.assertIn("약 이름 영어 괄호 빠짐", problems("아우벨리티가 나왔습니다.", "아우벨리티(Auvelity)는 줄였습니다.")[0])
        self.assertTrue(problems("배경", "아우벨리티는 증상을 줄였습니다."))
        # 코드명처럼 본문에도 영문 그대로 쓴 이름은 괄호를 요구하지 않는다.
        self.assertEqual(
            check(Summary(**{**GOOD, "summary_finding": "MW073은 행동을 줄였습니다."}),
                  [Drug(ko="MW073", en="MW073")], "The antagonist MW073 mitigates aggressive behavior"), [])
        # 초록에 없는 영어 이름(LLM이 지어낸 것)은 검사하지 않는다.
        self.assertEqual(problems("배경", "아우벨리티는 줄였습니다.", [Drug(ko="아우벨리티", en="Madeupname")]), [])

    def test_missing_drug_english_is_fixed_without_regeneration(self):
        paper_id = self.paper_id
        papers = [(paper_id, "Auvelity for agitation", "BACKGROUND: Auvelity ...")]
        drugs = [{"ko": "아우벨리티", "en": "Auvelity"}]
        bad = json.dumps({**GOOD, "summary_finding": "아우벨리티는 증상을 줄였습니다.", "drugs": drugs})

        client = FakeClient(bad)
        with self.assertLogs("summarize_papers", "INFO") as logs:
            report = run(papers, client, "fake-model")

        self.assertEqual((client.calls, len(report.done), report.failed, report.regenerated), (1, 1, [], []))
        self.assertIn("약 이름 괄호 자동 보정: 아우벨리티(Auvelity)", logs.output[0])
        self.assertIn("약 이름 영어 괄호 검사: 걸린 논문 0편", summarize_papers.to_markdown(report, "fake-model"))
        self.assertEqual(
            functions.get_paper_detail(paper_id).summary_finding, "아우벨리티(Auvelity)는 증상을 줄였습니다."
        )

    def test_other_problems_still_rejected_after_drug_fix(self):
        papers = [(self.paper_id, "Auvelity for agitation", "BACKGROUND: Auvelity ...")]
        bad = json.dumps({**GOOD, "summary_finding": "아우벨리티는 차이가 있었습니다(p < 0.05).",
                          "drugs": [{"ko": "아우벨리티", "en": "Auvelity"}]})

        report = run(papers, FakeClient(bad), "fake-model")

        self.assertEqual(report.done, [])
        self.assertIn("통계 표기", report.failed[0][2])
        self.assertNotIn("약 이름 영어 괄호", report.failed[0][2])
        self.assertIsNone(functions.get_paper_detail(self.paper_id).summary_finding)

    def test_flagged_summary_is_regenerated_once(self):
        bad = json.dumps({**GOOD, "summary_finding": "차이가 있었습니다(p < 0.05)."})

        client = FakeClient(bad, then=json.dumps(GOOD))
        report = run(self.papers, client, "fake-model")
        self.assertEqual((client.calls, len(report.done), report.failed), (2, 1, []))
        self.assertEqual([(n, passed) for n, _, passed in report.regenerated], [(report.done[0][0], True)])
        self.assertEqual(functions.get_paper_detail(self.paper_id).summary_finding, "발견")

    def test_summary_flagged_twice_is_not_saved(self):
        bad = json.dumps({**GOOD, "summary_finding": "차이가 있었습니다(p < 0.05)."})
        client = FakeClient(bad)

        report = run(self.papers, client, "fake-model")

        self.assertEqual((client.calls, report.done), (2, []))
        self.assertIn("통계 표기", report.failed[0][2])
        self.assertEqual([passed for *_, passed in report.regenerated], [False])
        self.assertIsNone(functions.get_paper_detail(self.paper_id).summary_finding)

    def test_polished_text_is_saved_and_changes_are_reported(self):
        draft = {**GOOD, "summary_finding": "위약보다 초조가 줄었습니다."}
        polished = {
            **GOOD,
            "summary_finding": "효과 없는 가짜 약보다 안절부절못하는 증상이 줄었습니다.",
            "changes": [
                {"before": "위약", "after": "효과 없는 가짜 약"},
                {"before": "글에 없던 말", "after": "안절부절못하는 증상"},  # LLM이 잘못 적은 경우
            ],
        }
        client = FakeClient(json.dumps(draft), polish=json.dumps(polished))
        report = run(self.papers, client, "fake-model", polish=True)

        self.assertEqual(client.calls, 2)  # 처음 쓰기 + 다듬기

        self.assertEqual(functions.get_paper_detail(self.paper_id).summary_finding, polished["summary_finding"])
        self.assertEqual(report.done[0][5], [
            ("위약", "효과 없는 가짜 약", True),
            ("글에 없던 말", "안절부절못하는 증상", False),
        ])
        markdown = summarize_papers.to_markdown(report, "fake-model")
        self.assertIn("- 위약 → 효과 없는 가짜 약\n", markdown)
        self.assertIn("(글에서 확인 안 됨)", markdown)

    def test_markdown_counts_explanations_on_common_disease_names(self):
        finding = (
            "치매(기억력이 떨어지는 병) 환자와 전두측두엽 치매(성격이 변하는 치매) 환자, "
            "경도인지장애(치매 전 단계) 환자를 살폈습니다."
        )
        client = FakeClient(json.dumps({**GOOD, "summary_finding": finding}))

        markdown = summarize_papers.to_markdown(run(self.papers, client, "fake-model", dry_run=True), "fake-model")

        self.assertIn("뒤에 괄호 설명이 붙은 곳: 1개", markdown)
        self.assertIn("번: 치매(기억력이 떨어지는 병)", markdown)

    def test_summary_api_key_prefers_dedicated_key(self):
        with mock.patch.multiple(summarize_papers.settings, openai_summary_api_key="전용", openai_api_key="공용"):
            self.assertEqual(summarize_papers.summary_api_key(), ("OPENAI_SUMMARY_API_KEY", "전용"))
        with mock.patch.multiple(summarize_papers.settings, openai_summary_api_key="", openai_api_key="공용"):
            self.assertEqual(summarize_papers.summary_api_key(), ("OPENAI_API_KEY", "공용"))

    def test_dry_run_does_not_write(self):
        report = run(self.papers, FakeClient(json.dumps(GOOD)), "fake-model", dry_run=True)

        self.assertEqual(len(report.done), 1)
        self.assertIsNone(functions.get_paper_detail(self.paper_id).summary_finding)
        self.assertIn(GOOD["title_ko"], summarize_papers.to_markdown(report, "fake-model"))

    def test_keeps_existing_study_type_and_evidence_level(self):
        functions.save_evidence([(self.paper_id, "rct", 2)])

        run(self.papers, FakeClient(json.dumps(GOOD)), "fake-model")

        saved = functions.get_paper_detail(self.paper_id)
        self.assertEqual((saved.study_type, saved.evidence_level), ("rct", "2"))
        self.assertEqual(saved.summary_finding, "발견")


class FixDrugEnglishTest(unittest.TestCase):
    """fix_drug_english(): 영어 괄호가 빠진 약 이름의 첫 등장 뒤에 "(영어)"를 붙인다 (DB 없이)."""

    SOURCE = "Brexpiprazole and donepezil for agitation in Alzheimer's disease"
    DRUGS = [Drug(ko="브렉스피프라졸", en="brexpiprazole"), Drug(ko="도네페질", en="donepezil")]

    def fix(self, background, finding, drugs=DRUGS):
        summary = Summary(**{**GOOD, "summary_background": background, "summary_finding": finding})
        return summarize_papers.fix_drug_english(summary, drugs, self.SOURCE)

    def test_fixes_one(self):
        fixed, notes = self.fix("배경", "브렉스피프라졸은 증상을 줄였고, 브렉스피프라졸을 오래 썼습니다.", self.DRUGS[:1])
        self.assertEqual(fixed.summary_finding, "브렉스피프라졸(brexpiprazole)은 증상을 줄였고, 브렉스피프라졸을 오래 썼습니다.")
        self.assertEqual(notes, ["브렉스피프라졸(brexpiprazole)"])
        self.assertEqual(check(fixed, self.DRUGS[:1], self.SOURCE), [])

    def test_fixes_several_at_first_mention_across_sections(self):
        fixed, notes = self.fix("도네페질을 쓰는 환자가 많습니다.", "브렉스피프라졸과 도네페질을 비교했습니다.")
        self.assertEqual(fixed.summary_background, "도네페질(donepezil)을 쓰는 환자가 많습니다.")
        self.assertEqual(fixed.summary_finding, "브렉스피프라졸(brexpiprazole)과 도네페질을 비교했습니다.")
        self.assertEqual(sorted(notes), ["도네페질(donepezil)", "브렉스피프라졸(brexpiprazole)"])
        self.assertEqual(check(fixed, self.DRUGS, self.SOURCE), [])

    def test_leaves_existing_parentheses_alone(self):
        summary = Summary(**{**GOOD, "summary_finding": "브렉스피프라졸(brexpiprazole)과 도네페질 (donepezil)을 썼습니다."})
        fixed, notes = summarize_papers.fix_drug_english(summary, self.DRUGS, self.SOURCE)
        self.assertIs(fixed, summary)
        self.assertEqual(notes, [])

    def test_other_parenthesis_is_not_touched_and_still_flagged(self):
        fixed, notes = self.fix("배경", "브렉스피프라졸(새 약)을 썼습니다.", self.DRUGS[:1])
        self.assertEqual((fixed.summary_finding, notes), ("브렉스피프라졸(새 약)을 썼습니다.", []))
        self.assertIn("약 이름 영어 괄호 빠짐", check(fixed, self.DRUGS[:1], self.SOURCE)[0])

    def test_other_checks_unchanged(self):
        fixed, _ = self.fix("배경", "브렉스피프라졸은 차이가 있었습니다(p < 0.05).", self.DRUGS[:1])
        problems = check(fixed, self.DRUGS[:1], self.SOURCE)
        self.assertEqual(len(problems), 1)
        self.assertIn("통계 표기", problems[0])


if __name__ == "__main__":
    unittest.main()
