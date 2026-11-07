"""Offline Feed policy and iterative research with the real shared Runner."""
from copy import deepcopy
import json
import traceback
import unittest
from unittest.mock import patch

from ..outputs import FeedAnswerV2, StructuredAgentResult
from ..prompts import DefaultPromptBuilder
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec, production_registry
from ..schemas import ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..testing.fake_tools import fake_registry
from ..tools.contracts import EvidenceItem, EvidencePackage
from ..workflows.feed_context_prepare import prepare_production_feed_input
from ..workflows.feed_execution import FeedExecutionV1, FeedExecutionError
from .test_feed_candidates import review_turn
from .test_feed_context_production_source import CONTEXT as SOURCE_CONTEXT, snapshot, context_loader
from ..workflows.feed_context_backend_source import ProductionFeedContextSource


CONTEXT = SOURCE_CONTEXT.model_copy(update={"request_id": "private-request-marker"})


def planning():
    source = ProductionFeedContextSource(read_snapshot=lambda **kwargs: snapshot())
    return context_loader(source)(CONTEXT, recent_days=30)


def prepared_input():
    source = ProductionFeedContextSource(read_snapshot=lambda **kwargs: snapshot())
    return prepare_production_feed_input(CONTEXT, context_loader(source))


def search_call(identifier="search_1", query="Dementia sleep research", *, candidate_count=None, retain=()):
    call = ToolCall(id=identifier, name="search_evidence", arguments={"query": query})
    return review_turn(ModelTurn(tool_calls=[call]), candidate_count, retain).tool_calls[0] if candidate_count is not None else call


def final_turn(*, count=1, selected=(0,), empty=False):
    return ModelTurn(text=json.dumps({"decisions": [
        {"include": True, "personal_reason": "Synthetic care interest", "category": "care"}
        if i in selected and not empty else {"include": False} for i in range(count)]}))


def research_runner(turns, *, empty_queries=(), client=None):
    """Feed execution owns the Runner configuration; no live RAG connection."""
    searches = []
    def search(context, args):
        searches.append((context, deepcopy(args)))
        return EvidencePackage(query=args.query, evidence=[] if args.query in empty_queries else [
            EvidenceItem(evidence_type="new_research", title="[FAKE] " + args.query,
                         relevance_score=0.8, pmid=("FAKE-PMID" if args.query == "Dementia sleep research"
                                                   else "FAKE-PMID-" + args.query), doi="FAKE-DOI",
                         abstract="Synthetic public evidence")])
    client = client if client is not None else FakeLLMClient(turns)
    execution = FeedExecutionV1(client, ToolRegistry((
        ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], search, test_only=True),
    ), mode="test"))
    return execution, client, searches


class FeedExecutionContractTests(unittest.TestCase):
    def test_extra_or_empty_registry_is_rejected_at_construction(self):
        for registry in (fake_registry(), production_registry()):
            client = FakeLLMClient([])
            with self.assertRaises(FeedExecutionError):
                FeedExecutionV1(client, registry)
            self.assertEqual(client.requests, [])

    def test_mutable_configuration_is_rechecked_before_execution(self):
        changes = (("prompt_builder", DefaultPromptBuilder()), ("registry", fake_registry()),
                   ("registry", production_registry()), ("max_tool_rounds", 5), ("max_total_tool_calls", 10))
        for attribute, value in changes:
            with self.subTest(attribute=attribute):
                execution, client, searches = research_runner([])
                setattr(execution.runner, attribute, value)
                with self.assertRaises(FeedExecutionError) as caught:
                    execution(CONTEXT, prepared_input())
                self.assertEqual(str(caught.exception), "Feed execution failed")
                self.assertEqual(client.requests, [])
                self.assertEqual(searches, [])

    def test_runner_executor_registry_mismatch_is_rejected_before_provider(self):
        for changed_side in ("runner", "executor"):
            for broad in (False, True):
                with self.subTest(changed_side=changed_side, broad=broad):
                    execution, client, searches = research_runner([])
                    replacement, _, _ = research_runner([])
                    registry = fake_registry() if broad else replacement.runner.registry
                    target = execution.runner if changed_side == "runner" else execution.runner.executor
                    target.registry = registry
                    with self.assertRaises(FeedExecutionError):
                        execution(CONTEXT, prepared_input())
                    self.assertEqual(client.requests, [])
                    self.assertEqual(searches, [])

    def test_unprepared_input_is_rejected_before_provider(self):
        for source in (None, {}, planning(), "", "  "):
            execution, client, searches = research_runner([])
            with self.assertRaises(FeedExecutionError):
                execution(CONTEXT, source)
            self.assertEqual(client.requests, [])
            self.assertEqual(searches, [])

    def test_runner_configuration_error_does_not_expose_underlying_data(self):
        execution, client, _ = research_runner([])
        with patch.object(execution.runner.registry, "names", side_effect=ValueError("PRIVATE configuration detail")):
            with self.assertRaises(FeedExecutionError) as caught:
                execution(CONTEXT, "prepared input")
        self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(client.requests, [])

class IterativeResearchTests(unittest.TestCase):
    def test_one_search_then_feed_returns_exact_runner_result_and_resolved_metadata(self):
        execution, client, searches = research_runner([ModelTurn(tool_calls=[search_call()]), final_turn()])
        returned = []
        original = execution.runner.run_structured
        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            returned.append(result)
            return result
        with patch.object(execution.runner, "run_structured", side_effect=capture) as run:
            prepared = prepared_input()
            result = execution(CONTEXT, prepared)
        self.assertEqual(run.call_args.args[0], prepared)
        self.assertIs(type(run.call_args.args[0]), str)
        self.assertEqual(run.call_args.kwargs["previously_recommended_pmids"], frozenset())
        self.assertIs(result, returned[0])
        self.assertIs(run.call_args.args[1], CONTEXT)
        self.assertIs(run.call_args.kwargs["output_model"], FeedAnswerV2)
        self.assertIsInstance(result, StructuredAgentResult)
        self.assertIsInstance(result.output, FeedAnswerV2)
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(result.execution.total_tool_calls, 1)
        self.assertEqual(result.execution.errors, [])
        self.assertIsNone(result.execution.final_answer)
        self.assertEqual(len(searches), 1)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(result.sources[0].title, "[FAKE] Dementia sleep research")
        self.assertEqual(result.sources[0].pmid, "FAKE-PMID")
        self.assertEqual(result.sources[0].doi, "FAKE-DOI")
        self.assertEqual(result.sources[0].source_ref, result.output.items[0].source_ref)

    def test_first_evidence_reaches_llm_before_it_chooses_a_second_query(self):
        test = self
        class EvidenceDrivenFake(FakeLLMClient):
            def generate(self, request):
                messages, tools = request.inputs, request.tools
                if len(self.requests) == 1:
                    tool = next(m for m in messages if m.type == "function_call_output")
                    data = json.loads(tool.content)
                    test.assertEqual(tool.call_id, "search_1")
                    test.assertTrue(data["success"])
                    candidates = json.loads(next(m.content for m in messages if m.kind == "evidence"))
                    test.assertEqual(candidates["candidates"][0]["title"], "[FAKE] Dementia sleep research")
                    # The synthetic provider chooses a followup after inspecting actual evidence.
                    self.turns.append(ModelTurn(tool_calls=[search_call("search_2", "Caregiver sleep support research", candidate_count=1, retain=(0,))]))
                elif len(self.requests) == 2:
                    self.turns.append(final_turn(count=2, selected=(1,)))
                return super().generate(request)
        client = EvidenceDrivenFake([ModelTurn(tool_calls=[search_call()])])
        execution, _, searches = research_runner([], client=client)
        result = execution(CONTEXT, prepared_input())
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual([args.query for _, args in searches],
                         ["Dementia sleep research", "Caregiver sleep support research"])
        self.assertEqual(len(client.requests), 3)
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type == "function_call_output"], ["search_1", "search_2"])
        self.assertEqual(result.sources[0].source_ref.tool_call_id, "search_2")

    def test_third_search_result_can_be_followed_by_normal_final_output(self):
        turns = [ModelTurn(tool_calls=[search_call(f"search_{i}", f"Research angle {i}", candidate_count=(i-1) or None, retain=range(i-1))]) for i in range(1, 4)]
        execution, client, searches = research_runner(turns + [final_turn(count=3, selected=(2,))])
        result = execution(CONTEXT, prepared_input())
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(result.execution.tool_rounds, 3)
        self.assertEqual(result.execution.total_tool_calls, 3)
        self.assertEqual(len(searches), 3)
        self.assertEqual(len(client.requests), 4)
        self.assertIn("search_3", [m.call_id for m in client.requests[-1][0] if m.type == "function_call_output"])

    def test_fourth_tool_call_is_not_executed(self):
        execution, client, searches = research_runner([
            ModelTurn(tool_calls=[search_call(f"search_{i}", f"Research perspective {i}", candidate_count=(i-1) or None, retain=range(min(i-1, 5)))]) for i in range(1, 5)])
        result = execution(CONTEXT, prepared_input())
        self.assertEqual(result.execution.status, "limit_reached")
        self.assertEqual(result.execution.total_tool_calls, 3)
        self.assertEqual(len(searches), 3)
        self.assertEqual(len(client.requests), 4)
        self.assertIsNone(result.output)
        self.assertEqual(result.sources, [])

    def test_multi_call_turns_share_total_budget_and_over_budget_batch_is_not_executed(self):
        for batches, count, rounds, status in (
            ((3,), 3, 1, "completed"), ((4,), 0, 0, "limit_reached"),
            ((2, 2), 4, 2, "completed"), ((2, 1), 3, 2, "completed")):
            with self.subTest(batches=batches):
                identifier, turns = 0, []
                for revision, batch in enumerate(batches):
                    calls = []
                    for _ in range(batch):
                        identifier += 1
                        calls.append(search_call(f"search_{identifier}", f"Research perspective {identifier}"))
                    turn = ModelTurn(tool_calls=calls)
                    turns.append(review_turn(turn, identifier-batch, range(identifier-batch)) if revision else turn)
                if status == "completed":
                    turns.append(final_turn(count=count))
                execution, _, searches = research_runner(turns)
                result = execution(CONTEXT, prepared_input())
                self.assertEqual(result.execution.status, status)
                self.assertEqual(result.execution.total_tool_calls, count)
                self.assertEqual(result.execution.tool_rounds, rounds)
                self.assertEqual(len(searches), count)

    def test_successful_empty_evidence_allows_followup_or_empty_feed(self):
        for followup in (False, True):
            with self.subTest(followup=followup):
                turns = [ModelTurn(tool_calls=[search_call()])]
                if followup:
                    turns += [ModelTurn(tool_calls=[search_call("search_2", "Different research perspective", candidate_count=0)]), final_turn()]
                else:
                    turns.append(final_turn(count=0))
                execution, client, searches = research_runner(turns, empty_queries={"Dementia sleep research"})
                result = execution(CONTEXT, prepared_input())
                tool = next(m for m in client.requests[1][0] if m.type == "function_call_output")
                self.assertTrue(json.loads(tool.content)["success"])
                candidates = json.loads(next(m.content for m in client.requests[1][0] if m.kind == "evidence"))
                self.assertEqual(candidates["candidate_count"], 0)
                self.assertEqual(result.execution.status, "completed")
                self.assertEqual(result.execution.errors, [])
                self.assertEqual(len(searches), 2 if followup else 1)
                self.assertEqual(len(result.output.items), 1 if followup else 0)

    def test_model_generated_call_reference_is_rejected_in_internal_feed_decision(self):
        for call_id, index in (("nonexistent_call", 0), ("search_1", 1)):
            draft = json.loads(final_turn().text)
            draft["decisions"][0]["source_ref"] = {"tool_call_id": call_id, "evidence_index": index}
            execution, _, _ = research_runner([ModelTurn(tool_calls=[search_call()]), ModelTurn(text=json.dumps(draft))])
            with self.subTest(call_id=call_id, index=index):
                result = execution(CONTEXT, prepared_input())
                self.assertEqual(result.execution.status, "invalid_response")
                self.assertEqual(result.execution.errors[-1].code, "invalid_feed_decision")
                self.assertIsNone(result.output)
                self.assertEqual(result.sources, [])

    def test_provider_failure_and_invalid_response_never_return_normal_feed(self):
        for turn, status, code in (
            (RuntimeError("PRIVATE provider detail"), "provider_error", "provider_error"),
            (ModelTurn(text="PRIVATE invalid JSON"), "invalid_response", "invalid_feed_json"),
            (ModelTurn(text=""), "invalid_response", "empty_response"),
            (ModelTurn.model_construct(text=123, tool_calls=[]), "invalid_response", "invalid_model_turn")):
            with self.subTest(status=status, code=code):
                execution, _, _ = research_runner([ModelTurn(tool_calls=[search_call()]), turn])
                result = execution(CONTEXT, prepared_input())
                self.assertEqual(result.execution.status, status)
                self.assertEqual(result.execution.errors[-1].code, code)
                self.assertIsNone(result.output)
                self.assertEqual(result.sources, [])
                self.assertNotIn("PRIVATE", result.model_dump_json())



if __name__ == "__main__":
    unittest.main()
