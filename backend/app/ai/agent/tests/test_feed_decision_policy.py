"""Policy delivery and scripted execution paths, NOT real LLM judgment quality."""
import json
import unittest

from ..feed_candidates import build_feed_decision_policy
from ..schemas import ModelTurn
from ..testing.fake_llm import FakeLLMClient
from ..tools.search_evidence import SEARCH_EVIDENCE_DATA_POLICY
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.feed_prompts import FeedPromptBuilder
from . import test_parallel_feed as parallel
from .test_feed_candidates import package, decisions
from .test_feed_review_rounds import search
from .test_workflows import CONTEXT


class FeedDecisionPolicyTests(unittest.TestCase):
    setUp = parallel.ParallelFeedTests.setUp

    def run_feed(self, turns, pools, prepared=None):
        reached = []
        def handler(context, args):
            reached.append((args.query, args.top_k))
            return pools[args.query]
        client = FakeLLMClient(turns)
        prepared = prepared or json.dumps({'feed_scope': 'caregiver_all', 'patients': [
            {'patient_ref': 'p1', 'stage': 'MCI', 'symptoms': ['낮잠 증가', '수면장애']}]})
        result = FeedExecutionV1(client, parallel.registry(handler))(CONTEXT, prepared)
        return result, client, reached

    @staticmethod
    def first_pools():
        # Synthetic candidates deliberately include different research populations.
        pools = {key: package([f'{key}-{i}' for i in range(5)]) for key in ('patient', 'family', 'general')}
        for key, source in pools.items():
            for row in source.evidence:
                row.title = {'patient': 'Sleep and cognition in people with MCI',
                             'family': 'Insomnia treatment in family caregivers',
                             'general': 'Sleep in general adult shift workers'}[key]
                row.abstract = row.title + ': synthetic evidence, no clinical claim'
        return pools

    def test_initial_policy_prioritizes_evidence_sufficiency_over_minimal_search_count(self):
        policy = FeedPromptBuilder().build_system_prompt(CONTEXT)
        final = build_feed_decision_policy().content
        for clause in ('검색 횟수 최소화 자체를 목표로 하지 않는다', '중요한 연구 목적의 충족 여부',
                       '추가 검색 가치와 종료 타당성', '편수만으로 자동 종료/재검색하지 않는다',
                       '이 평가는 기존 호출 안에서 수행', '의미적으로 유사한 검색은 허용한다'):
            self.assertNotIn(clause, policy)
            self.assertIn(clause, final)
        self.assertIn('적합한 3~5편으로 주요 목적이 충족되면 첫 라운드도 종료 가능하다', final)
        self.assertIn('1~2편도 추가 검색 실익이 낮으면 종료 가능하다', final)
        self.assertIn('첫 검색을 반드시', policy)
        self.assertNotIn('일반적인 목표는 검색 생성 1회와 최종 판단 1회', policy)
        self.assertNotIn('decisions', policy)  # Post-search schema stays out of the planning policy.

    def test_individual_fit_distinguishes_actual_population_purpose_and_result(self):
        policy = build_feed_decision_policy().content
        for clause in ('연구 대상·목적·실제 결과와 Context의 연결',
                       'MCI 환자의 낮잠 증가만으로 보호자의 불면증 치료 연구',
                       '보호자 본인의 수면 문제나 지원 필요성이 실제 Context',
                       '대상군·단계·환경이 다르다는 이유만으로 일괄 제외하거나 미제공 상태를 추측하지 않는다',
                       '실제 연구 결과가 personal_reason을 뒷받침',
                       '개입 연구라는 이유만으로 우선하거나 연관성 연구를 일괄 낮게 평가하지 않는다'):
            self.assertIn(clause, policy)
        for clause in ('질환 단계, 연구 환경', '차이가 있다면 적용 한계를 구분한다',
                       '관찰연구의 연관성을 인과관계나 치료 효과 입증으로 바꾸지 않는다'):
            self.assertIn(clause, SEARCH_EVIDENCE_DATA_POLICY)

    def test_combination_compares_information_value_without_forcing_diversity(self):
        policy = build_feed_decision_policy().content
        for clause in ('추천 조합의 실질적 정보 중복과 상호 보완성', '중요한 연구 목적의 충족 여부',
                       'PMID가 달라도 같은 연관성만 반복', '같은 주제라도 대상·방법·결과의 추가 정보가 상호 보완적이면 함께 추천',
                       '더 직접적인 후보 대신 간접 관련 연구', '주제/카테고리별 할당량이나 최소 추천 개수는 없다'):
            self.assertIn(clause, policy)

    def test_zero_result_requires_value_consideration_but_remains_legal(self):
        policy = build_feed_decision_policy().content
        for clause in ('현재 추천 조합이 충분한가와 추가 검색이 실질적으로 개선할 가능성이 있는가를 구분',
                       '0편 선택 예정이면 대상/목적 불일치와 근거 부족을 구분',
                       '보호자/일반 인구 연구만 나왔다는 사실을 환자 관련 근거의 부재로 확정하지 않는다',
                       '미제시 기존 결과·유지 후보와 남은 예산', '기대 가치가 낮으면 0편 종료도 허용',
                       '편수만으로 자동 종료/재검색하지 않는다', '검색 횟수·후보 수·추천 수를 채우지 않는다',
                       '판단 과정이나 별도 평가 필드 없이 Tool 호출 또는 최종 JSON만 반환'):
            self.assertIn(clause, policy)
        self.assertLess(policy.index('개별 연구 적합성'), policy.index('추천 조합의 유용성'))
        self.assertLess(policy.index('추천 조합의 유용성'), policy.index('추가 검색 가치와 종료 타당성'))

    def test_three_one_two_or_zero_selections_can_stop_after_first_round(self):
        for selected in ((0, 3, 6), (0,), (0, 3), ()):
            with self.subTest(selected=selected):
                result, client, reached = self.run_feed([search(['patient', 'family', 'general'], 'first'),
                    ModelTurn(text=json.dumps(decisions(10, selected)))], self.first_pools())
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls, len(client.requests)), (1, 3, 2))
                self.assertEqual([o.tool_choice for o in client.options], ['required', 'auto'])
                self.assertEqual(len(result.output.items), len(selected))
                self.assertCountEqual(reached, [('patient', 8), ('family', 8), ('general', 8)])
                last = json.loads(client.requests[-1][0][-1].content)
                self.assertEqual((last['candidate_count'], last['search_progress']['search_calls_remaining']), (10, 6))
                policy = next(m.content for m in client.requests[-1][0] if m.role == 'system' and 'FeedDecisionDraft' in (m.content or ''))
                self.assertIn('0편 선택 예정이면', policy)
                self.assertIn('추천 조합의 실질적 정보 중복', policy)
                for row in result.output.items:
                    self.assertEqual(set(row.model_dump()), {'source_ref', 'personal_reason', 'category'})

    def test_scripted_mixed_patient_and_caregiver_selection_preserves_actual_source(self):
        # Script chooses patient rows 0 and 3; this does NOT prove the model can identify them.
        result, client, _ = self.run_feed([search(['patient', 'family', 'general'], 'first'),
            ModelTurn(text=json.dumps(decisions(10, (0, 3))))], self.first_pools())
        self.assertEqual([s.pmid for s in result.sources], ['patient-0', 'patient-1'])
        self.assertEqual([(s.source_ref.tool_call_id, s.source_ref.evidence_index) for s in result.sources],
                         [('feed-rag:1:0:0', 0), ('feed-rag:1:0:0', 1)])
        self.assertEqual([i.personal_reason for i in result.output.items], ['Reason 0', 'Reason 3'])
        self.assertEqual(len(client.requests), 2)

    def test_followup_can_refine_same_topic_and_retain_an_earlier_source(self):
        pools = self.first_pools()
        pools['patient sleep intervention in MCI'] = package(['new-0', 'new-1', 'new-2', 'patient-0'])
        result, client, reached = self.run_feed([search(['patient', 'family', 'general'], 'first'),
            search(['patient sleep intervention in MCI'], 'next', 10, (0,)),
            ModelTurn(text=json.dumps(decisions(9, (0, 5))))], pools)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((len(client.requests), result.execution.total_tool_calls), (3, 4))
        self.assertEqual([s.pmid for s in result.sources], ['patient-0', 'new-1'])
        self.assertEqual([(s.source_ref.tool_call_id, s.source_ref.evidence_index) for s in result.sources],
                         [('feed-rag:1:0:0', 0), ('feed-rag:2:0:0', 1)])
        latest = json.loads(client.requests[-1][0][-1].content)
        self.assertEqual((latest['retained_count'], latest['candidate_count']), (1, 9))
        self.assertEqual(latest['candidates'][0]['status'], 'retained')
        self.assertNotIn('evidence_excerpt', latest['candidates'][0])
        self.assertEqual(len({s.pmid for s in result.sources}), 2)
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type == "function_call_output"], ['first', 'next'])
        self.assertEqual(reached[-1], ('patient sleep intervention in MCI', 8))

    def test_nine_empty_searches_allow_final_zero_and_reject_fourth_round(self):
        for extra_round in (False, True):
            with self.subTest(extra_round=extra_round):
                turns = [search([f'r{r}-{i}' for i in range(3)], f'provider-{r}', 0 if r else None) for r in range(3)]
                turns.append(search(['extra'], 'fourth', 3) if extra_round else ModelTurn(text=json.dumps(decisions(0))))
                pools = {f'r{r}-{i}': package([]) for r in range(3) for i in range(3)}
                result, client, reached = self.run_feed(turns, pools)
                self.assertEqual(result.execution.status, 'limit_reached' if extra_round else 'completed')
                self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls, len(client.requests)), (3, 9, 4))
                self.assertEqual(len(reached), 9)
                self.assertEqual(client.options[-1].tool_choice, 'none')
                self.assertEqual(result.sources, [])
                if extra_round: self.assertIsNone(result.output)
                else: self.assertEqual(result.output.items, [])

    def test_six_includes_still_fail_and_final_json_has_no_evaluation_metadata(self):
        for extra_field in (False, True):
            payload = decisions(10, (0,) if extra_field else range(6))
            if extra_field: payload['evaluation'] = {'stop_reason': 'should not be exposed'}
            result, client, _ = self.run_feed([search(['patient', 'family', 'general'], 'first'),
                ModelTurn(text=json.dumps(payload))], self.first_pools())
            self.assertEqual(result.execution.status, 'invalid_response')
            self.assertEqual(result.execution.errors[-1].code, 'invalid_feed_decision' if extra_field else 'too_many_selected')
            self.assertIsNone(result.output)
            self.assertEqual(result.sources, [])
            self.assertEqual(len(client.requests), 2)

    def test_cold_start_and_multiple_patient_context_are_not_rewritten(self):
        for patients in ([], [{'patient_ref': 'p1', 'symptoms': ['수면 문제']}, {'patient_ref': 'p2', 'symptoms': ['넘어짐']} ]):
            prepared = json.dumps({'feed_scope': 'caregiver_all', 'patients': patients}, ensure_ascii=False)
            result, client, _ = self.run_feed([search(['patient'], 'first'),
                ModelTurn(text=json.dumps(decisions(5, (0,))))], self.first_pools(), prepared)
            self.assertEqual(result.execution.status, 'completed')
            for messages, _ in client.requests:
                self.assertEqual([m.content for m in messages if m.kind == 'user_input'], [prepared])
