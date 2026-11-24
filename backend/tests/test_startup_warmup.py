"""main.warm_chat_tokenizer — 서버 시작 때 챗봇 토크나이저 예열. 실패해도 예외 없이 경고만."""
import unittest
from unittest.mock import patch

import main


class WarmChatTokenizerTests(unittest.TestCase):
    def warm(self, model="gpt-4o", **counter):
        with patch.object(main.chat, "chat_model", return_value=model), \
             patch.object(main, "chat_history_token_counter", **counter) as load:
            main.warm_chat_tokenizer()
        return load

    def test_loads_encoding_for_chat_model(self):
        with self.assertLogs("uvicorn.error", "INFO") as logs:
            load = self.warm(return_value=len)
        load.assert_called_once_with("gpt-4o")
        self.assertIn("예열 완료", logs.output[0])

    def test_failure_only_warns(self):
        for name, counter in (("예외", {"side_effect": OSError("down")}),
                              ("UTF-8 대체", {"return_value": main.utf8_token_upper_bound})):
            with self.subTest(name), self.assertLogs("uvicorn.error", "WARNING") as logs:
                self.warm(**counter)
            self.assertIn("예열 실패", logs.output[0])

    def test_skips_without_chat_model(self):
        self.warm(model="").assert_not_called()

    def test_lifespan_warms_tokenizer_and_server_starts_on_failure(self):
        """서버 시작(lifespan) 때 예열을 부르고, 토크나이저 로드가 실패해도 서버는 뜬다."""
        from fastapi.testclient import TestClient

        with patch.object(main.chat, "chat_model", return_value="gpt-4o"), \
             patch.object(main, "chat_history_token_counter", side_effect=OSError("down")) as load, \
             self.assertLogs("uvicorn.error", "WARNING"), TestClient(main.app) as client:
            self.assertEqual(client.get("/openapi.json").status_code, 200)
        load.assert_called_once_with("gpt-4o")


if __name__ == "__main__":
    unittest.main()
