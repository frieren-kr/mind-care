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
        self.settings = SimpleNamespace(
            openai_api_key="offline-placeholder", openai_summary_model="gpt-5.6-luna", openai_agent_model="")
        for target, options in {
            "app.api.recommendations.settings": {"new": self.settings},
            # Windows asyncio의 내부 socketpair는 허용하고 실제 HTTP 전송만 차단한다.
            "httpx.HTTPTransport.handle_request": {"side_effect": AssertionError("Live HTTP forbidden")},
            "httpx.AsyncHTTPTransport.handle_async_request": {"side_effect": AssertionError("Live HTTP forbidden")},
            "psycopg.connect": {"side_effect": AssertionError("Live DB forbidden")},
        }.items():
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_key_or_model_never_creates_sdk(self):
        for key, agent, summary in (
            ("", "agent-model", "summary-model"),
            (" \n", "agent-model", ""),
            ("offline-placeholder", "", ""),
            ("offline-placeholder", " \n", "  "),
        ):
            with self.subTest(key_present=bool(key.strip()), agent=agent, summary=summary), \
                    patch.object(recommendations, "OpenAI") as sdk:
                recommendations.get_feed_execution.cache_clear()
                self.settings.openai_api_key = key
                self.settings.openai_agent_model = agent
                self.settings.openai_summary_model = summary
                self.assertIsNone(recommendations.get_feed_execution())
                sdk.assert_not_called()

    def test_agent_model_precedence_and_summary_fallback(self):
        for agent, summary, expected in (
            ("agent-model", "summary-model", "agent-model"),
            ("agent-model", "", "agent-model"),
            ("", "summary-model", "summary-model"),
            (" \n", " summary-model ", "summary-model"),
            (" agent-model ", "summary-model", "agent-model"),
        ):
            with self.subTest(agent=agent, summary=summary), \
                    patch.object(recommendations, "OpenAI") as sdk, \
                    patch.object(recommendations, "OpenAIClient") as client, \
                    patch.object(recommendations, "FeedExecutionV1") as execution:
                recommendations.get_feed_execution.cache_clear()
                self.settings.openai_agent_model = agent
                self.settings.openai_summary_model = summary
                self.assertIs(recommendations.get_feed_execution(), execution.return_value)
                client.assert_called_once_with(sdk.return_value, model=expected, timeout_seconds=20.0)
                self.assertEqual(self.settings.openai_summary_model, summary)

    def test_sdk_initialization_failure_falls_back_without_logging_secrets(self):
        with patch.object(recommendations, "OpenAI", side_effect=RuntimeError("PRIVATE key")), \
                self.assertLogs(recommendations.logger, "WARNING") as logs:
            self.assertIsNone(recommendations.get_feed_execution())
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("PRIVATE", "\n".join(logs.output))

    def test_live_http_is_blocked_for_sync_and_async_clients(self):
        import asyncio
        import httpx

        with httpx.Client(trust_env=False) as client:
            with self.assertRaisesRegex(AssertionError, "Live HTTP forbidden"):
                client.get("https://example.invalid")

        async def check_async_client():
            async with httpx.AsyncClient(trust_env=False) as client:
                with self.assertRaisesRegex(AssertionError, "Live HTTP forbidden"):
                    await client.get("https://example.invalid")

        asyncio.run(check_async_client())

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
        self.assertEqual(settings.openai_agent_model, "")
        self.assertEqual(settings.openai_api_key, "")
        self.assertEqual(Settings.model_config["env_file"], Path(__file__).resolve().parents[1] / ".env")

    def test_dotenv_model_and_key_with_environment_precedence(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("OPENAI_API_KEY=offline-placeholder\nOPENAI_SUMMARY_MODEL=gpt-5.6-luna\n", encoding="utf-8")
            settings = Settings(_env_file=path)
            self.assertEqual(settings.openai_api_key, "offline-placeholder")
            self.assertEqual(settings.openai_summary_model, "gpt-5.6-luna")
            self.assertEqual(settings.openai_agent_model, "")
            with patch.dict(os.environ, {"OPENAI_SUMMARY_MODEL": "configured-test-model"}):
                self.assertEqual(Settings(_env_file=path).openai_summary_model, "configured-test-model")

    def test_agent_model_dotenv_with_environment_precedence(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("OPENAI_AGENT_MODEL=agent-model\nOPENAI_SUMMARY_MODEL=summary-model\n", encoding="utf-8")
            settings = Settings(_env_file=path)
            self.assertEqual(settings.openai_agent_model, "agent-model")
            self.assertEqual(settings.openai_summary_model, "summary-model")
            with patch.dict(os.environ, {"OPENAI_AGENT_MODEL": "environment-agent-model"}):
                settings = Settings(_env_file=path)
                self.assertEqual(settings.openai_agent_model, "environment-agent-model")
                self.assertEqual(settings.openai_summary_model, "summary-model")
