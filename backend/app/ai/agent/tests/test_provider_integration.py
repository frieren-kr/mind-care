"""명시적인 opt-in + 기존 설정의 API key/model이 있을 때만 외부 요청한다."""
import os
import unittest

from ..clients.openai_client import OpenAIClient
from ..agent_loop_runner import AgentLoopRunner
from ..registry import production_registry
from .test_workflows import CONTEXT


@unittest.skipUnless(os.environ.get("MINDCARE_RUN_PROVIDER_INTEGRATION") == "1",
                     "Optional live Provider test is disabled")
class ProviderIntegrationTests(unittest.TestCase):
    def test_live_provider(self):
        from openai import OpenAI
        from app.config import settings

        model = os.environ.get("MINDCARE_AGENT_MODEL")
        if not settings.openai_api_key or not model:
            self.skipTest("Existing API key binding and explicit model are required")
        with OpenAI(api_key=settings.openai_api_key, timeout=20, max_retries=0) as sdk:
            result = AgentLoopRunner(OpenAIClient(sdk, model=model, timeout_seconds=20), production_registry()).run(
                "연결 확인입니다. 짧게 인사해 주세요.", CONTEXT)
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.final_answer)
