"""통합 배치(scripts/update_papers.py) 테스트. 단계 함수와 준비 확인을 가짜로 바꿔서 DB·PubMed·OpenAI·모델 없이 돈다.

실행 (backend/ 에서):  python -m unittest tests.test_update_papers
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import update_papers


def ok(args):
    return 3, 0, ""


def boom(args):
    raise RuntimeError("단계 통째 실패")


class UpdatePapersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "run.lock"
        self.calls = []
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(update_papers, "check_ready", return_value=None).start()

    def stages(self, **override):
        """단계마다 호출을 기록하는 가짜를 끼운다. override로 특정 단계의 동작을 바꾼다."""
        def recorder(name, fn):
            def stage(args):
                self.calls.append(name)
                return fn(args)
            return stage

        fakes = {name: recorder(name, override.get(name, ok)) for name in update_papers.STAGES}
        mock.patch.dict(update_papers.STAGES, fakes).start()

    def run_main(self, *argv):
        return update_papers.main(["--lock-file", str(self.lock), *argv])

    def test_all_ok_exit_0_in_order(self):
        self.stages()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.calls, ["collect", "embed", "classify", "summarize"])

    def test_stage_failure_continues_and_exit_1(self):
        self.stages(embed=boom)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.calls, ["collect", "embed", "classify", "summarize"])

    def test_partial_failure_exit_1(self):
        self.stages(summarize=lambda a: (2, 1, ""))
        self.assertEqual(self.run_main(), 1)

    def test_not_ready_exit_2_runs_nothing(self):
        self.stages()
        update_papers.check_ready.return_value = "DB 연결 실패"
        self.assertEqual(self.run_main(), 2)
        self.assertEqual(self.calls, [])

    def test_missing_key_is_not_ready(self):
        mock.patch.stopall()  # 진짜 check_ready로 확인 (키 없음은 DB를 보기 전에 걸린다)
        with mock.patch("scripts.summarize_papers.summary_api_key", return_value=("OPENAI_API_KEY", "")):
            self.assertIn("키가 없습니다", update_papers.check_ready(["summarize"]))

    def test_second_run_exits_while_locked(self):
        self.stages()
        held = update_papers.acquire_lock(self.lock)
        self.assertIsNotNone(held)
        try:
            self.assertIsNone(update_papers.acquire_lock(self.lock))
            self.assertEqual(self.run_main(), 0)
            self.assertEqual(self.calls, [])  # 아무 단계도 돌지 않고 바로 끝난다
        finally:
            held.close()
        self.assertEqual(self.run_main(), 0)  # 잠금이 풀리면 다시 돈다
        self.assertEqual(len(self.calls), 4)

    def test_skip_collect_and_only(self):
        self.stages()
        self.run_main("--skip-collect")
        self.assertEqual(self.calls, ["embed", "classify", "summarize"])
        self.calls.clear()
        self.run_main("--only", "classify")
        self.assertEqual(self.calls, ["classify"])

    def test_log_file(self):
        self.stages()
        log = Path(self.tmp.name) / "logs" / "u.log"
        self.run_main("--log-file", str(log))
        text = log.read_text(encoding="utf-8")
        self.assertIn("[요약] 처리 3건, 실패 0건", text)
        self.assertIn("종료 코드 0", text)
        update_papers.setup_logging(None)  # 파일 핸들러를 닫아 임시 폴더를 지울 수 있게 한다


class StageFunctionTest(unittest.TestCase):
    """embed / classify 단계가 DB 함수와 AI 함수를 순서대로 부르고 건수를 세는지 (모두 가짜)."""

    def test_embed_loops_until_empty(self):
        from types import SimpleNamespace as NS

        paper = NS(id="p1", title="t", abstract="a")
        get = mock.Mock(side_effect=[[paper], []])
        save = mock.Mock(return_value=NS(saved=1, not_found=[]))
        with mock.patch.object(update_papers.functions, "get_papers_without_embedding", get), \
                mock.patch.object(update_papers.functions, "save_embeddings", save), \
                mock.patch("app.ai.retrieval.embedding.embed_documents", return_value=[[0.0] * 1024]), \
                mock.patch("app.ai.retrieval.embedding.free_memory"):
            done, failed, _ = update_papers.stage_embed(NS(dry_run=False))
        self.assertEqual((done, failed), (1, 0))
        self.assertEqual(save.call_args.args[0][0][0], "p1")

    def test_classify_stops_when_nothing_saved(self):
        from types import SimpleNamespace as NS

        paper = NS(paper_id="p1", publication_types=["Review"], mesh_terms=[], title="t", abstract="a")
        with mock.patch.object(update_papers.functions, "get_papers_without_evidence", return_value=[paper]), \
                mock.patch.object(update_papers.functions, "save_evidence",
                                  return_value=NS(saved=0, not_found=["p1"])):
            with self.assertRaises(RuntimeError):  # 무한 반복하지 않고 멈춘다
                update_papers.stage_classify(NS(dry_run=False))


class SkipFailedTest(unittest.TestCase):
    """요약에 MAX_FAILS번 실패한 논문을 상태 파일에 기록하고 다음 실행부터 건너뛰는지 (DB·OpenAI 모두 가짜)."""

    def setUp(self):
        from types import SimpleNamespace as NS

        self.NS = NS
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self.papers = [NS(id=f"id{i}", external_id=f"pmid{i}", title="t", abstract="a") for i in range(3)]
        self.fail_ids = {"id0"}  # 이 논문만 매번 요약 실패
        self.sent = []  # run()에 넘어간 id 목록 (실행마다)
        for target, kw in [
            ("scripts.summarize_papers.summary_api_key", {"return_value": ("OPENAI_API_KEY", "k")}),
            ("scripts.summarize_papers.run", {"side_effect": self.fake_run}),
            ("openai.OpenAI", {}),
        ]:
            self.enterContext(mock.patch(target, **kw))
        self.get = self.enterContext(mock.patch.object(
            update_papers.functions, "get_new_papers", side_effect=lambda limit: self.papers[:limit]))

    def fake_run(self, papers, client, model, dry_run=False):
        ids = [p[0] for p in papers]
        self.sent.append(ids)
        return self.NS(
            done=[(n, i, "t", "a", None, None) for n, i in enumerate(ids, 1) if i not in self.fail_ids],
            failed=[(n, i, "ValueError: 검사 실패") for n, i in enumerate(ids, 1) if i in self.fail_ids],
            no_abstract=0, prompt_tokens=0, completion_tokens=0,
        )

    def summarize(self, limit=10, retry=False, dry_run=False):
        args = self.NS(state_file=self.state, retry_skipped=retry, dry_run=dry_run, limit=limit)
        return update_papers.stage_summarize(args)

    def fails(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["id0"]["fails"]

    def test_skip_after_max_fails(self):
        for _ in range(update_papers.MAX_FAILS):
            self.assertEqual(self.summarize()[1], 1)
        self.assertEqual(self.fails(), update_papers.MAX_FAILS)
        with self.assertLogs("update_papers", "INFO") as logs:
            done, failed, note = self.summarize()
        self.assertEqual((done, failed), (2, 0))  # 건너뛴 건 실패로 안 센다 → 종료 코드 1의 원인 아님
        self.assertNotIn("id0", self.sent[-1])
        self.assertIn("건너뜀 1건 (pmid0)", "\n".join(logs.output))
        self.assertIn("건너뜀 1", note)

    def test_skipped_does_not_take_limit_slot(self):
        self.state.write_text(json.dumps({"id0": {"fails": 3, "pmid": "pmid0"}}), encoding="utf-8")
        self.summarize(limit=2)
        self.assertEqual(self.sent[-1], ["id1", "id2"])

    def test_retry_skipped(self):
        self.state.write_text(json.dumps({"id0": {"fails": 3, "pmid": "pmid0"}}), encoding="utf-8")
        self.fail_ids = set()  # 이번엔 성공
        self.summarize(retry=True)
        self.assertIn("id0", self.sent[-1])
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8")), {})  # 성공하면 기록이 지워진다

    def test_broken_or_missing_state_starts_fresh(self):
        self.summarize()  # 파일 없음
        self.assertEqual(self.fails(), 1)
        self.state.write_text("{깨진 json", encoding="utf-8")
        with self.assertLogs("update_papers", "WARNING"):
            self.assertEqual(self.summarize()[1], 1)
        self.assertEqual(self.fails(), 1)

    def test_dry_run_does_not_write_state(self):
        self.summarize(dry_run=True)
        self.assertFalse(self.state.exists())

    def test_default_state_file_next_to_log(self):
        log = Path(self.tmp.name) / "logs" / "u.log"
        seen = {}
        with mock.patch.object(update_papers, "run_stages", side_effect=lambda a: seen.setdefault("a", a) and 0):
            update_papers.main(["--log-file", str(log), "--lock-file", str(Path(self.tmp.name) / "l")])
        update_papers.setup_logging(None)
        self.assertEqual(seen["a"].state_file, log.parent / update_papers.STATE_NAME)


if __name__ == "__main__":
    unittest.main()
