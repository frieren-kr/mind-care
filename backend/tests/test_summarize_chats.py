"""scripts/summarize_chats 테스트. DB 없이 chat_store 함수를 Mock으로 바꿔 돌린다.

실행 (backend/ 에서):  python -m unittest tests.test_summarize_chats
"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.db.chat_store import KST
from scripts import summarize_chats

DAY = date(2026, 11, 5)
U1, U2, P1 = uuid4(), uuid4(), uuid4()


class SummarizeChatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "run.lock"
        self.store = {}
        for name, value in {"list_chat_threads_for_day": [(U1, P1), (U2, None)],
                            "get_messages_for_day": ["메시지"], "save_daily_summary": None,
                            "delete_old_chat_messages": 3}.items():
            patcher = patch.object(summarize_chats.chat_store, name, return_value=value)
            self.store[name] = patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("psycopg.connect", side_effect=AssertionError("Live DB forbidden"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *argv):
        return summarize_chats.main(["--lock-file", str(self.lock), "--date", DAY.isoformat(), *argv])

    def test_without_summarizer_skips_summary_but_deletes(self):
        self.assertEqual(self.run_main(), 0)
        self.store["save_daily_summary"].assert_not_called()
        self.store["delete_old_chat_messages"].assert_called_once_with(7)

    def test_summarizes_each_thread_then_deletes(self):
        with patch.object(summarize_chats, "summarize_day", return_value=("요약", "m")):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.store["save_daily_summary"].call_args_list,
                         [((U1, P1, DAY, "요약", "m"),), ((U2, None, DAY, "요약", "m"),)])
        self.store["list_chat_threads_for_day"].assert_called_once_with(DAY)
        self.store["delete_old_chat_messages"].assert_called_once()

    def test_one_failure_is_exit_1_and_others_continue(self):
        with patch.object(summarize_chats, "summarize_day", side_effect=[RuntimeError("PRIVATE"), ("요약", "m")]):
            with self.assertLogs("summarize_chats", "WARNING") as logs:
                self.assertEqual(self.run_main(), 1)
        self.store["save_daily_summary"].assert_called_once()
        self.store["delete_old_chat_messages"].assert_called_once()  # 보관 기간은 요약 실패와 상관없이 지킨다
        self.assertNotIn("PRIVATE", "\n".join(logs.output))

    def test_delete_failure_is_exit_1(self):
        self.store["delete_old_chat_messages"].side_effect = RuntimeError
        self.assertEqual(self.run_main(), 1)

    def test_db_unavailable_is_exit_2(self):
        self.store["list_chat_threads_for_day"].side_effect = OSError
        self.assertEqual(self.run_main(), 2)
        self.store["delete_old_chat_messages"].assert_not_called()

    def test_dry_run_writes_nothing(self):
        with patch.object(summarize_chats, "summarize_day", return_value=("요약", "m")):
            self.assertEqual(self.run_main("--dry-run"), 0)
        self.store["save_daily_summary"].assert_not_called()
        self.store["delete_old_chat_messages"].assert_not_called()

    def test_default_date_is_yesterday_kst(self):
        # UTC로는 11/5 16:00, 한국은 11/6 01:00 → 어제 = 11/5
        fake_now = datetime(2026, 11, 6, 1, 0, tzinfo=KST)
        with patch.object(summarize_chats, "datetime", wraps=datetime) as dt:
            dt.now.return_value = fake_now
            summarize_chats.main(["--lock-file", str(self.lock)])
        self.store["list_chat_threads_for_day"].assert_called_once_with(DAY)

    def test_second_run_exits_while_locked(self):
        held = summarize_chats.acquire_lock(self.lock)
        try:
            self.assertEqual(self.run_main(), 0)
            self.store["list_chat_threads_for_day"].assert_not_called()
        finally:
            held.close()
        self.assertEqual(self.run_main(), 0)
        self.store["list_chat_threads_for_day"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
