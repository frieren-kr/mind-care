"""요약 배치(scripts/summarize_papers.py) 테스트. 가짜 LLM 클라이언트를 쓰고, 실 DB가 떠 있어야 한다.

실행 (backend/ 에서):  python -m unittest tests.test_summarize_papers

OpenAI를 부르지 않는다. 임시 논문(source='test-summarize')을 넣고 끝나면 지운다
(paper_analysis는 ON DELETE CASCADE로 같이 지워진다). 007 마이그레이션이 적용돼 있어야 한다.
"""
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4

from app.db import functions
from app.db.connection import get_connection
from app.schemas import PaperIn
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
        # 초록에 없는 영어 이름(LLM이 지어낸 것)은 검사하지 않는다.
        self.assertEqual(problems("배경", "아우벨리티는 줄였습니다.", [Drug(ko="아우벨리티", en="Madeupname")]), [])

    def test_missing_drug_english_triggers_regeneration(self):
        paper_id = self.paper_id
        papers = [(paper_id, "Auvelity for agitation", "BACKGROUND: Auvelity ...")]
        drugs = [{"ko": "아우벨리티", "en": "Auvelity"}]
        bad = json.dumps({**GOOD, "summary_finding": "아우벨리티는 증상을 줄였습니다.", "drugs": drugs})
        good = json.dumps({**GOOD, "summary_finding": "아우벨리티(Auvelity)는 증상을 줄였습니다.", "drugs": drugs})

        client = FakeClient(bad, then=good)
        report = run(papers, client, "fake-model")

        self.assertEqual((client.calls, len(report.done), report.failed), (2, 1, []))
        self.assertIn("약 이름 영어 괄호", report.regenerated[0][1][0])
        markdown = summarize_papers.to_markdown(report, "fake-model")
        self.assertIn("약 이름 영어 괄호 검사: 걸린 논문 1편 [1], 재생성 1회, 재생성 뒤에도 걸려 최종 실패 0편", markdown)
        self.assertIn("금지 단어 검사: 걸린 논문 0편", markdown)
        self.assertEqual(
            functions.get_paper_detail(paper_id).summary_finding, "아우벨리티(Auvelity)는 증상을 줄였습니다."
        )

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


if __name__ == "__main__":
    unittest.main()
