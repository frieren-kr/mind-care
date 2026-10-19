"""AI 실행기 설정/조립 검증. 실제 키, DB, 외부 네트워크를 사용하지 않는다."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.api import recommendations
from app.config import Settings


class FeedExecutionDependencyTests(unittest.TestCase):
    def setUp(self):
        recommendations.get_feed_execution.cache_clear()
        self.addCleanup(recommendations.get_feed_execution.cache_clear)
        self.settings = SimpleNamespace(openai_api_key="offline-placeholder", openai_summary_model="gpt-5.6-luna")
        for target, options in {
            "app.api.recommendations.settings": {"new": self.settings},
            "socket.socket.connect": {"side_effect": AssertionError("Live network forbidden")},
            "psycopg.connect": {"side_effect": AssertionError("Live DB forbidden")},
        }.items():
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_key_or_model_never_creates_sdk(self):
        for key, model in (("", "gpt-5.6-luna"), (" \n", "gpt-5.6-luna"), ("offline-placeholder", "  ")):
            with self.subTest(key_present=bool(key.strip()), model=model), patch.object(recommendations, "OpenAI") as sdk:
                recommendations.get_feed_execution.cache_clear()
                self.settings.openai_api_key, self.settings.openai_summary_model = key, model
                self.assertIsNone(recommendations.get_feed_execution())
                sdk.assert_not_called()

    def test_sdk_initialization_failure_falls_back_without_logging_secrets(self):
        with patch.object(recommendations, "OpenAI", side_effect=RuntimeError("PRIVATE key")), \
                self.assertLogs(recommendations.logger, "WARNING") as logs:
            self.assertIsNone(recommendations.get_feed_execution())
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("PRIVATE", "\n".join(logs.output))

    def test_failed_execution_assembly_closes_sdk(self):
        sdk = Mock()
        with patch.object(recommendations, "OpenAI", return_value=sdk), \
                patch.object(recommendations, "FeedExecutionV1", side_effect=RuntimeError("PRIVATE setup")), \
                self.assertLogs(recommendations.logger, "WARNING"):
            self.assertIsNone(recommendations.get_feed_execution())
        sdk.close.assert_called_once_with()


class FeedSettingsLoadingTests(unittest.TestCase):
    def test_model_requires_configuration_and_backend_env_location(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.openai_summary_model, "")
        self.assertEqual(settings.openai_api_key, "")
        self.assertEqual(Settings.model_config["env_file"], Path(__file__).resolve().parents[1] / ".env")

    def test_dotenv_model_and_key_with_environment_precedence(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("OPENAI_API_KEY=offline-placeholder\nOPENAI_SUMMARY_MODEL=gpt-5.6-luna\n", encoding="utf-8")
            settings = Settings(_env_file=path)
            self.assertEqual(settings.openai_api_key, "offline-placeholder")
            self.assertEqual(settings.openai_summary_model, "gpt-5.6-luna")
            with patch.dict(os.environ, {"OPENAI_SUMMARY_MODEL": "configured-test-model"}):
                self.assertEqual(Settings(_env_file=path).openai_summary_model, "configured-test-model")
