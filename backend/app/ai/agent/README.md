# AgentLoopRunner (Fake 단계)

사용자 입력 → LLM Tool 선택 → 입력 검증 → 순차 Handler 실행 → 출력 검증/JSON →
Tool Result → LLM 재판단 → 추가 Tool 또는 최종 응답을 구현한다.

`AgentLoopRunner`는 provider-independent LLM + Tool Calling 반복 실행기다. LLM 호출,
Tool Call 수신, ToolExecutor 실행과 Tool Result 전달, 반복 상한/timeout/deadline 관리,
최종 응답 반환 및 structured output 실행을 담당한다. Chat/Feed Workflow 선택, 개인화 Context 선택,
Long-term Memory 로딩 정책, Guardrail 정책 결정, Workflow persistence 정책은 이 실행기의 책임이 아니다.

공용 실행기의 검증은 계약/Fake 중심이다. Feed Stage 1에는 실제 Backend Snapshot을 DI로 연결하는
Production Source와 오프라인 통합 테스트가 있으며, 실 DB 검증·HTTP 활성화·RAG/의료 규칙·챗봇 API 완성은 별도다.

## ChatWorkflow: 세 단계 Skeleton

`workflows/chat.py`는 Feed를 상속하지 않고 다음 세 단계만 실행한다.

```text
Stage 1 context_prepare:
  trusted AgentContext + 현재 사용자 질문 → 검증된 최소 JSON 문자열
Stage 2 chat_execution:
  요청별 Registry snapshot + ChatPromptBuilder + 기존 AgentLoopRunner
  → run_structured(output_model=ChatAnswerV1) 1회
  → Tool 0개 / 동일 Turn 복수 조회 / 결과를 본 후속 조회 → StructuredAgentResult
Stage 3 backend_handoff:
  기존 wrapper 중첩 재검증 → completed Chat + 현재 request 확인 → 필수 Backend callable
```

**Context Prepare.** `prepare_chat_input(context, user_message)`는 기존 AgentContext를
재검증하고 strict/nonblank 질문 및 4,000자 상한을 검사한다. 공통 ContentText의 공백·제어·서식
문자만 있는 입력 거부를 재사용하며 허용한 원문을 strip/요약/재작성하지 않는다.
공개 JSON 필드는 `user_message`, 서버 계산 `patient_scope_available` 두 개뿐이다.
user/patient/request UUID·ID는 자동 공개하지 않고 환자 전체 정보·CareLog·RAG·대화 이력도
사전 조회하지 않는다. 환자 선택 None은 일반 질문/검색 가능한 정상 요청이다.
Scope flag나 UUID 타입은 인증·환자 소속 검증의 증거가 아니며 trusted Backend가 이를 보장해야 한다.
Workflow의 JSON object 검사는 형식 guard이며 계약 검증은 주입한 신뢰된 Stage 1 책임이다.
임의 Raw JSON 주입은 지원 입력 경로가 아니다. 사용자 문장 속 개인정보를 비식별화하지 않는다.

**Chat 실행과 Registry.** `ChatExecutionV1(client, registry_factory, ...)`는 요청마다
`registry_factory(context)`를 호출하고 기존 canonical ToolRegistry 검증으로 별도 mapping을
조립한다. Wrapper에는 Runner/Registry 요청 상태를 보관하지 않으며 공용 Runner/Executor는
동일한 요청별 Registry를 사용한다. Factory는 허용 Tool과 실제 권한을 결정하고 patient=None이면
get_patient_profile/get_recent_care_logs를 노출하지 않아야 한다. 잘못된 Scope 조립은 Provider 호출
전에 거부한다. 기존 Executor의 None Scope 사전 차단은 그대로 남는다.
Tool DTO에 ownership ID가 없으면 유효한 모양의 다른 환자 정보를 Executor가 식별할 수 없으므로
실제 Handler에서 authorization/ownership을 반드시 확인해야 한다. Fake는 이를 보장하지 않는다.
향후 환자 전용 Tool은 Factory/Chat의 Scope 정책과 Executor guard에 함께 등록한다.
현재 Tool 전체 목록을 Workflow에 선언하거나 Registry V2/미래 Tool을 미리 구현하지 않는다.
Factory/Handler의 공유 서비스나 Client는 서버 조립부 책임이며 요청별 변경 상태를 보관하면 안 된다.

Chat은 공용 기본 예산 5 rounds / 10 calls를 사용한다. 동일 Turn의 독립 Tool들은 기존 Runner가
순차 실행해 결과를 모은 뒤 다음 LLM 호출로 전달한다. 세 Tool은 1 round / 3 calls이며 중간 LLM
재호출은 없다. 앞선 결과가 필요한 검색은 후속 Turn에서 LLM이 판단한다. Intent classifier,
별도 Planner/Tool loop/ParallelToolExecutor는 없다. Feed의 3/3 설정·검색 전용 Registry는 변경하지 않는다.
ChatPromptBuilder는 DefaultPromptBuilder의 Tool followup을 재사용하고 raw AgentContext 초기 메시지만
제외한다. 공용 Runner가 기존 Chat structured policy를 삽입한다. Tool/질문/Evidence는 비신뢰 데이터다.
최소 Tool 선택·기록과 임상 판단 구분·개인정보 query 복사 금지는 Prompt 정책이며 Sanitizer/Query Guard나
응답의 임상적 정확성을 강제하는 검증은 아니다. 실제 Provider 의미적 동작은 아직 검증하지 않았다.

**Handoff와 오류.** 필수 `backend_handoff(context, StructuredAgentResult)`에 검증된 별도 객체를
전달하고 Backend 소유 Receipt를 그대로 반환한다. 완료 상태, ChatAnswerV1, request_id,
legacy final_answer 일치, citation_refs/sources 순서 및 중첩 유효성을 기존 wrapper로 재검증한다.
검색 참조의 성공 여부/범위와 metadata는 기존 Runner/Evidence Resolver가 검증한다.
Tool 없는 직접 답변은 citation_refs=[] / sources=[]가 정상이다. 실패 status·잘못된 출처·변조
결과·Feed 출력은 Handoff에 전달하지 않는다. Recoverable Tool error 후 검증된 completed 답변은
errors를 보존해 전달하며 Tool timeout 등의 failed 결과는 차단한다. Backend가 객체를 수정해도
Runner 원본 결과는 바뀌지 않는다. 미구현 저장이 성공을 반환하는 default는 없다.
각 단계 exception은 고정 ChatWorkflowError.stage로 전환하며 원문/내부 exception 메시지는 숨긴다.
자동 retry/repair가 없으며 suppressed chaining은 APM traceback locals 제거까지 보장하지 않는다.

조립 인터페이스:

```python
execution = ChatExecutionV1(client, registry_factory)
workflow = ChatWorkflow(context_prepare=prepare_chat_input,
                        chat_execution=execution, backend_handoff=backend_handoff)
receipt = workflow.run(trusted_context, user_message)
```

`testing/chat_workflow_fakes.py`는 기존 Fake LLM/Tool과 실제 Chat 단계/Runner를 연결한다.
실제 Chat API, authorization, history, Patient/Medication/Visit Handler, RAG 연결, OpenAI Provider
통합 검증, Chat persistence 및 citation paper ID mapping은 후속이다. 기존 OpenAI client 구현과
recommendations의 PMID mapping은 존재하지만 ChatWorkflow와 연결하지 않았다.
향후 권한 검증된 history는 Stage 1의 데이터로 추가하며 과거 발화/Assistant 응답을 System 정책으로
승격하지 않는다. History selection, access/budget 정책, citation/persistence 책임을 장래 세분화할 수
있지만 현재 Stage는 세 개이고 Memory Manager/새 실행 프레임워크는 도입하지 않았다.

오프라인 검증 (`backend/`, 실제 Provider/DB 사용 없음):

```bash
MINDCARE_RUN_PROVIDER_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest app.ai.agent.tests.test_chat_workflow app.ai.agent.tests.test_chat_execution app.ai.agent.tests.test_workflows app.ai.agent.tests.test_outputs
```

## FeedWorkflow: 정식 Production Stage 1과 세 단계 실행

Feed ownership은 간병인/user다. 정식 Stage 1은 전체 관리 환자 0~20명을 서로 구분하며
`feed_scope="caregiver_all"` 하나만 사용한다. 여러 환자를 합성 환자로 병합하지 않는다.

```text
Stage 1 context_prepare:
  Backend get_feed_context_snapshot() → ProductionFeedContextSource
  → FeedContextSourceSnapshot → DefaultFeedContextLoader
  → FeedPersonalizationContextV1 → prepare_production_feed_input() → 최소 JSON
Stage 2 feed_execution:
  같은 FeedExecutionV1 → AgentLoopRunner.run_structured(output_model=FeedAnswerV1) 1회
  → LLM query ↔ search_evidence → FeedAnswerV1 / StructuredAgentResult
Stage 3 backend_handoff:
  completed + FeedAnswerV1 + 현재 request 확인 → 기존 wrapper 재검증 → 필수 Backend Handoff
```

**실행 Context와 Cold Start.** 기존 `AgentContext`의 user_id는 유효한 UUID 필수이며,
patient_id 필드는 필수이나 명시적 None을 허용한다. UUID만으로 인증을 대신하지 않는다.
환자 전용 get_patient_profile/get_recent_care_logs는 유효한 환자 Scope가 없으면 Handler 전에
기존 patient_context_mismatch로 거부하고 Runner는 failed로 종료한다. search_evidence는 None Scope에서도 실행한다.
저장된 간병인 프로필 없음/관리 환자 없음은 정상 Cold Start다. 사용자 없음/auth/read 실패는 오류다.

**Context 준비.** 필수 `context_prepare(context) -> str`은 신뢰된 서버 준비 함수다.
Workflow는 DTO 타입을 분기하거나 공개 필드를 재선언하지 않고 JSON object 형식만 확인한다.
임의 Raw JSON을 이 dependency로 주입하는 것은 지원되는 입력 경로가 아니다.
`workflows/feed_context_prepare.py`의 prepare_production_feed_input은 DefaultFeedContextLoader를
통해 user_id 기준 전체 관리 환자를 읽는다. 환자 없음은 정상 Cold Start이며 1~20명은 전부
포함하고, 20명 초과는 전체 실패한다. patient_id가 지정되어도 Production 필터로 쓰지 않는다.
Source identity/complete managed set/시간/Coverage 검증은 유지하며 Projection 전에 전체 V1을 재검증한다.

공통 공개 필드 선택·직렬화와 고정 `FeedProfileMappingError`는 feed_context_prepare.py가 소유한다.
정식 Prepare는 Demo Mapper/DTO를 import하거나 사용하지 않는다. Provider JSON 필드·값·키 순서와
ensure_ascii=False/allow_nan=False 정책을 유지하며 `feed_scope="caregiver_all"`을 고정한다.
caregiver relationship/lifestyle_tags와 환자별 ref/stage/symptoms/interests/일반화 안전 label/assessment_type만 공개한다.
점수·날짜·medication/visit/note·CareLog content·장기 summary·Coverage는 내부 Context에 유지한다.
Provider 입력에서 제외된 정보는 미제공이며 정상 0건이나 임상적 부재를 뜻하지 않는다.
자유 텍스트 비식별화·Query Guard는 구현하지 않았고 Allowlist 최소화가 PII-safe 승인은 아니다.

**Cold Start 출력.** 개인화가 비어도 일반 치매·인지건강·간병 연구 query를 생성하고
검색 Evidence가 있으면 연구 뉴스를 만들 수 있다. personal_reason은 필수 문자열을 유지하며,
개인화가 없으면 근거의 일반적인 정보적 관련성을 설명한다. 환자 상태를 꾸미지 않는다.
적절한 Evidence가 없거나 출력 계약을 충족할 수 없으면 items=[]를 허용한다.

**Feed 실행.** `workflows/feed_execution.py`의 `FeedExecutionV1(client, registry, ...)`가
FeedPromptBuilder와 3 rounds / 3 total calls를 지정하여 공용 Runner를 조립한다.
Feed 허용 Tool은 정확히 search_evidence 하나다. 생성 시와 실행 직전 실제 Executor와 Runner가
동일한 Registry를 사용하고, 공개 Tool·Prompt·budget이 Feed 계약을 지키는지 확인한다.
Workflow에는 별도 Runner, Retrieval Planner, Plan parameter, agent_stage 또는 미구현
run_feed_agent_loop seam이 없다. Wrapper는 공용 Runner를 요청당 한 번 호출하며,
Tool 반복/timeout/structured parser/evidence resolution은 공용 엔진에 위임한다.

**Backend Handoff.** 필수 `backend_handoff(context, result)` callable에 기존 V1 output과
server-resolved sources를 함께 넘긴다. 기존 StructuredAgentResult validator로 nested 계약과
출처 참조 순서를 재검증하고 completed FeedAnswerV1 및 현재 request의 결과만 전달한다.
다른 LLM 호출, 새로운 V2 DTO, source resolver, 임의 paper_id/metadata 생성은 없다.
정상 empty Feed는 허용한다. 실패 status/invalid output/출처 오류는 Handoff를 호출하지 않는다.
Recoverable Tool error 뒤 Runner가 검증된 completed 결과를 반환하면 기존 의미대로 전달하며
execution.errors를 보존한다. tool_timeout 등의 failed 결과는 전달하지 않는다.

각 단계의 exception은 `FeedWorkflowError.stage`(context_prepare/feed_execution/backend_handoff)로
감싸며 원문 메시지는 숨긴다. 이후 단계는 호출하지 않고 자동 retry/repair는 없다.
Handoff는 필수 주입이므로 미구현 Backend 저장을 성공으로 가장하는 default는 없다.
DB receipt/API DTO/실제 저장·권한·idempotency·추가 임상 filtering 정책은 Backend 연결 계약이 필요하다.
`testing/feed_workflow_fakes.py`는 실제 Production Source/Loader/Prepare/Execution/Runner를
합성 FeedReadSnapshot read/LLM/검색/Handoff와
연결하며 success receipt는 test-only다. 가짜 Plan, legacy text → 합성 Feed item 변환은 제거했다.

**Production 데이터 계약.** FeedPersonalizationContextV1이 유일한 정식 Context다.
정식 Prepare가 준비한 문자열만 Workflow에 주입한다. Raw Context DTO 주입은 거부한다.
외부 recommendations API의 기존 Demo 함수는 임시 호환 shim으로만 남으며 정식 경로가 아니다.
Production Source로 실제 Backend 함수→Stage 1→Workflow 연결을 오프라인 검증한다.
실 DB 실행 검증·Query Guard/RAG/Backend persistence 및 HTTP 활성화는 후속이다.
실환자 외부 Provider 전송은 privacy/provider review 전까지 production NO-GO다.

오프라인 통합 검증 (`backend/`):

```bash
MINDCARE_RUN_PROVIDER_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest app.ai.agent.tests.test_feed_workflow -v
```

### FeedPersonalizationContext V1

`workflows/feed_context.py`는 caregiver/user-scoped **입력** 계약이다. 공통 실행 `schemas.py`,
Tool 계약 `tools/contracts.py`, 최종 응답 `outputs.py`와 책임을 분리한다. Loader는 이 전체 계약으로
최종 검증하고 정식 Prepare가 최소 JSON을 Production Prompt/Runner로 전달한다. 최상위는 `schema_version="1"`, aware `reference_time`과 다음 네 구성이다.

- `caregiver_profile`: 저장된 간병인 프로필, row가 없으면 명시적 `null`
- `managed_patient_profiles`: 여러 관리 환자의 저장 프로필 및 환자별 종속 collection
- `recent_care_context`: 최근 정확히 30×24시간의 관찰 기록
- `long_term_summary`: optional caregiver/user-scoped derived 장기 요약, 없으면 명시적 `null`

모든 선언 필드는 required이며 nullable scalar도 생략하지 않고 `null`, 조회된 빈 list는
`[]`로 표현한다. Extra field를 거부한다. 사용자 ID, raw 환자 UUID, 간병인/환자 이름,
전화번호, 병원명 및 직접 식별정보를 위한 필드는 없다. 자유 텍스트 내부 PII 제거까지
schema가 보장하는 것은 아니며 서버 내부 `AgentContext` 계약도 변경하지 않았다.

| 모델 / DB 출처 | 포함 필드 및 의미 |
|---|---|
| `CaregiverProfileContext` / `caregiver_profiles` | `relationship`, `burden_score`, `mood_score`, `lifestyle_tags`, aware `updated_at`. 관계는 저장된 보조 TEXT이며 가족/전문간병인 분류나 특정 관리 환자와의 관계를 확정하지 않는다. 두 score는 strict int/null 원문이며 척도 버전·범위·방향·임상 기준을 보장하지 않는다. |
| `ManagedPatientContext` / `patient_profiles` | `patient_ref`, `dementia_stage`, `diagnosis_date`, `symptoms`, `interests`, aware `updated_at`과 아래 네 종속 collection. Stage는 저장값이고 `[]`는 미등록이지 무증상/관심 없음의 증거가 아니다. 수정 시각은 임상 평가 시각이 아니다. |
| `ClinicalAssessmentContext` / `clinical_assessments` | `assessment_type`, `score: Decimal/null`, `result_detail`, `assessed_at: date`. Unknown type을 그대로 허용하며 type/검사 버전/범위/판정 기준 없이 score로 단계·중증도·악화를 추론하지 않는다. |
| `SafetyEventContext` / `safety_events` | `event_date: date`, `has_fall`, `has_wandering`, `has_missing`, `note`. False는 해당 row의 flag일 뿐이며 날짜의 row 부재는 사건 없음의 증거가 아니다. |
| `MedicationContext` / `medications` | `drug_name`, `is_taking`, nullable `start_date`, `end_date`, `note`. Dosage/frequency는 제외한다. |
| `MedicalVisitContext` / `medical_visits` | `visit_date: date`, `is_visited`, `department`, `visit_content`. `hospital_name`은 제외한다. |
| `CareLogContext` / `care_logs` | nullable `patient_ref`, `log_type`, `content`, `mood_tag`, aware `logged_at`. Null ref는 이 Context에서 환자와 연결되지 않았다는 뜻이며 caregiver self-care로 단정하지 않는다. |
| `LongTermPersonalizationSummary` / `profile_summaries` | date `period_start`, `period_end`, nullable `summary`, `updated_tags`, aware `generated_at`. 간병인/user의 derived 개인화 맥락이며 특정 환자의 장기 임상 memory가 아니다. |

DB의 같은 이름 컬럼을 위 필드에 대응시키고 DB row ID, `assessed_by`, 불필요한
created/updated metadata는 포함하지 않는다. `patient_ref`만 Loader가 DB patient ID를
Context-local opaque reference(예: `patient_1`)로 변환한다. 영구 identity나 중요도 순번이 아니다.

**Schema validation / Loader boundary.** `ContextCollection[T]`는 `items`와 `coverage`
(`total_count`, `included_count`, `is_truncated`)로 구성한다. 두 count는 strict non-negative int이며
`included_count == len(items) <= total_count`, `is_truncated == (included_count < total_count)`를 강제한다.
Total은 동일 논리적 snapshot에서 authorization과 정의된 eligibility/basic filter를 통과한 수다.
아래 Alignment의 E/I 의미를 따른다. `is_truncated`는 SQL LIMIT 유무가 아니라 eligible 대비
실제 inclusion/selection 누락 여부다. Coverage 숫자만으로 DB 전체 history를 추론하지 않는다.
빈 `items`, count 0/0, false는 정상 조회 결과다. DB/Loader 실패를 empty/null로 위장하지 않는다.
`care_logs.coverage`는 **선택된 관리 환자 집합 + eligibility상 허용된 비귀속 기록** 범위의 완전성이다.
False여도 간병인의 모든 환자 기록이 포함되었다는 뜻은 아니다. 조회 limit/선택 알고리즘은 payload에 없다.

Context 안의 `patient_ref`는 unique이고 모든 non-null log ref는 포함된 관리 환자를 가리켜야 한다.
Truncated 환자 collection에도 이 규칙을 적용한다. Loader는 먼저 선택 환자 집합을 확정하고
DB ID ↔ ref의 일대일 대응과 같은 환자 소속의 clinical/safety/medication/visit record 조립을
보장해야 한다. Payload에는 raw ID가 없어 validator가 DB 소속/권한을 증명할 수 없다.
선택되지 않은 환자의 기록을 다른 ref에 붙이거나 원래 ref를 null로 바꿔 비귀속으로 위장하면 안 된다.
`care_logs.patient_id`의 `ON DELETE SET NULL`로 원래 비귀속/삭제 환자 기록을 현재 값만으로
구분하지 못할 수 있다. 삭제 환자 기록 eligibility는 아래 미확정 정책으로 남긴다.

모든 datetime은 aware여야 한다. `reference_date`는 payload가 아니라 서비스 timezone의
`reference_time` 날짜이며 date-only 필드와 비교한다. Loader/Context 계약은 generic `tzinfo` DI를
유지하고 기본 UTC/한국 시간을 선택하지 않는다. 현재 Production composition은 Backend의
canonical `app.user_schemas.APP_TIMEZONE`(KST)과 동일한 timezone을 binding해야 한다.
Backend 기준이 바뀌면 composition도 함께 바꾼다. 서버는 아래처럼 validation context에
`tzinfo`를 공급해야 하며 없으면 validation failure다. 실제 Production wiring은 아직 없다.

```python
validated = FeedPersonalizationContextV1.model_validate(
    payload, context={"service_timezone": agreed_service_timezone},
)
```

개별 하위 모델의 validation 성공은 전체 V1 Context 계약 충족을 의미하지 않는다.
`reference_time`에 의존하는 temporal rule과 관리 환자/CareLog cross-reference는 최상위
Context에서 검증한다. Context Loader/Assembler는 완성된 payload를 LLM에 전달하기 전에
반드시 위 `FeedPersonalizationContextV1.model_validate()`로 최종 검증하고,
`context={"service_timezone": agreed_service_timezone}`을 명시적으로 전달해야 한다.

Diagnosis/assessment/safety 날짜와 summary 종료일은 `reference_date` 이후일 수 없다.
Medication은 양 날짜가 있으면 start ≤ end만 강제하고 미래 날짜를 허용한다.
`is_taking=true`와 과거 종료일/미래 시작일의 의미 충돌은 **유효한 conflicting data**로 보존하며
LLM이 한 값을 선택하거나 note로 덮어쓰지 않는다. 미래 visit + `is_visited=true`는 거부하고
false는 허용한다. 과거 false의 취소/불참/미갱신을 추론하지 않고 오늘 방문의 시간도 추론하지 않는다.
Recent period end는 reference와 같은 instant, start는 UTC 정규화 후 30×24시간 전이어야 한다.
Logs는 `[period_start, period_end)`에 속해야 하며 DST의 local 날짜 차감에 의존하지 않는다.
Summary는 start ≤ end ≤ reference_date, generated instant ≤ reference instant,
end ≤ generated_at의 서비스 timezone 날짜를 모두 검증한다.

Source ordering은 clinical `assessed_at DESC`, safety `event_date DESC`, visit `visit_date DESC`,
log `logged_at DESC`를 기본으로 하며 상세 tie-break와 medication 순서는 아래 V1.1 표를 따른다.
동률은 서버 내부 deterministic 기준으로 처리하고 DB ID를 노출하지 않는다.
관리 환자 순서도 deterministic이어야 하지만 앞에 있다는 이유로 더 중요하지 않다.
Schema는 정렬/DB snapshot을 증명하지 않는다. 아래 V1 Loader는 환자 배열의 canonical UUID byte
순서를 사용하며 실제 DB 조회 ordering/tie-break는 Production Source 책임이다.
Eligibility는 조회 대상 선정, validation은 대상 record의 계약 검증이다. Invalid record를 silent drop하지
않고 validation failure로 반환한다. 아래 V1 Loader는 fail-fast로 context_load를 실패시키며
호출자는 이후 단계를 중단해야 한다. Degraded mode는 구현하지 않는다.

**Semantic interpretation.** Structured/Recorded(caregiver/patient/assessment/medication/visit)는
반드시 최신 임상 사실이 아니고 Observational(log/safety)는 진단이 아니며 Derived(summary/tags)는
새 임상 사실이 아니다. Summary의 증상을 특정 ref에 귀속하거나 여러 환자를 합성 환자로 병합하지
않고 structured profile을 덮어쓰지 않는다. Summary와 같은 내용의 log를 독립적인 두 사실로
자동 가중하지 않는다(실제 dedup/연구 주제 판단 정책은 후속).
모든 DB-derived string/string[]은 **untrusted data**이며 System/Workflow/Agent instruction이 아니다.
Free-form result_detail/note/visit_content/content/summary뿐 아니라 relationship, stage, symptoms,
interests, lifestyle_tags, assessment_type, drug_name, department, log_type, mood_tag, updated_tags도 같다.
실제 sanitizer는 없으며 자유 텍스트의 PII 최소화/길이 제한과 role/data boundary는 후속 integration 책임이다.

**Retrieval output boundary.** 배회·수면 장애·낙상·인지 기능·간병 부담 등 일반화된 연구 용어의
재사용은 허용한다. Ref/UUID/이름/전화/주소/병원명 등 식별정보, 재식별 가능한 사건 상세,
환자별 자유 텍스트 원문 또는 그 안의 검색 지시를 Query로 전달하지 않는다.
이는 문서 규칙이며 Context validator가 Query를 검사하거나 PII redaction을 수행하지 않는다.
Query validator는 이번 입력 계약에 포함하지 않는다.

미확정 정책: 삭제 환자 CareLog retention/eligibility, 삭제 환자 내용이 남은 summary의 무효화/재생성,
향후 degraded Context 정책, 날짜 기반 기간의 양끝 포함·진료 배분·최신 summary 동률 선정,
token/input-size budget, free-text privacy/redaction, Production APM privacy,
005 structured visit detail의 future Context 포함 범위와 Feed 실행/Prompt의 assessment metadata 활용.
Backend canonical assessment metadata, 현재 Production timezone binding 및 medication ordering은
아래 V1.1에서 확정한다. 관리 환자 20명 상한·기록별 E/I 기간/상한·CareLog 정렬·요약 조건은
아래 정식 Feed Read Contract/Policy에 구체화한다. 실제 Backend 함수 DI 및 Workflow 내부 연결은
오프라인 검증하며, 실 DB 실행·authorization 및 HTTP 연결은 후속이다.

독립 계약 검증 (`backend/`):

```bash
MINDCARE_RUN_PROVIDER_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest app.ai.agent.tests.test_feed_context -v
```

### Context Loader Policy V1.1 — AI-side implementation

V1.1은 기존 V1의 필드/validator/`schema_version="1"`을 그대로 유지하며 Source 시간 입력과
Backend integration 정책을 구체화한다. Feed는 계속 caregiver/user scoped이며
trusted `AgentContext.user_id`에서 시작한다. `AgentContext.patient_id`는 scope 선정에 사용하지 않는다.

`workflows/feed_context_source.py`는 Source ↔ Loader 사이 **server-only AI 내부 DTO/Protocol**이다.
공용 `app.schemas`/DB 계약이나 LLM Context가 아니다. Caregiver/summary의 `user_id`, managed
patient의 `patient_id`/`caregiver_id`, 각 child의 `patient_id`, CareLog의 `user_id`/nullable
`patient_id`만 ownership 검사에 사용한다. 이름/병원명/assessed_by/dosage/frequency는 Source에도 없다.
`SourceCollection[T]`의 `items`, strict non-negative `total_count`는 required이고
`len(items) <= total_count`다. Total은 authorization + eligibility + temporal/base filter 이후,
inclusion/selection 이전의 eligible 수이며 빈 성공은 명시적 `items=[]`, `total_count=0`이다.

`DefaultFeedContextLoader`는 keyword-only `source`, `clock`, `service_timezone`을 필수 주입받는다.
`__call__(context, recent_days=30)`만 허용하며 다른 값은 Source/clock 호출 전에 `invalid_request`다.
Clock을 한 번 호출해 datetime/aware/유효한 offset을 확인한 **후** UTC로 정규화한다.
Reference와 period end는 같은 instant이고 start는 `reference - timedelta(hours=720)`이다.
Service timezone은 window 계산이 아닌 Source selection용 `reference_date` 계산과 기존
date-only validation에 사용하며 기본값은 없다. Loader 내부에 KST를 하드코딩하지 않는다.

`reference_date = reference.astimezone(service_timezone).date()`를 같은 clock instant에서 계산한 뒤
`FeedContextSource.load_snapshot(*, user_id, reference_time, reference_date, recent_period_start,
recent_period_end)`를 한 번 호출한다. `reference_time`은 UTC instant 기준이고 `reference_date`는
service local calendar date다. `reference_date`는 server-only Source selection metadata다.
Snapshot/최종 Context에 `reference_date` 필드를 추가하지 않는다. Source는 전달된 모든 시간값을
조회·time-based eligibility에 실제 사용하며 자체 `datetime.now()`/`date.today()`나 임의 clock으로
재계산하지 않는다. UTC 2026-10-09 16:00이면 KST date는 2026-10-10, start는 UTC
2026-09-09 16:00, end는 같은 reference instant다. DST에서도 window는 elapsed 720시간이다.

Loader는 반환 Snapshot/child instance도 재검증하고 다음 Source invariant를 검사한다.
Caregiver/summary/log user와 patient caregiver는 `context.user_id`와 같아야 한다.
Managed patient는 eligible 전체 포함(`total_count == len(items)`), raw ID unique/non-null이어야 한다.
네 child collection의 ID는 enclosing patient와 같고 non-null CareLog ID는 managed 집합에 있어야 한다.
실제 DB authorization이나 snapshot correctness를 증명하는 검사는 아니다.

환자를 `patient_id.bytes` 순으로 정렬해 `patient_1`, `patient_2`, …를 부여하며 최종 환자 배열도
같은 순서다. Source 환자 순서가 달라도 동일 집합의 mapping/배열은 같다. 임상 중요도/영속 ID/
완전 익명화가 아닌 Context-local pseudonymous ref이며 집합이 달라지면 번호가 바뀔 수 있다.
Child/log 순서는 Source대로 보존하고 정렬·ranking·monotonicity 검사를 새로 넣지 않는다.
아래 stable ordering과 DB tie-break는 Production Source가 구현한다. Source 환자 순서는 중요도/ranking이
아니며 Backend `list_patients()`의 `created_at, id` 순서가 Loader의 UUID bytes canonical sort를 대체하지 않는다.
Unknown CareLog ID를 null로 바꾸지 않으며 **Source의 원래 null만** null ref로 복사한다.
삭제 환자 provenance/retention은 추론하지 않는다.

각 collection의 total을 그대로 복사하고 included는 실제 길이, truncated는 included < total로 계산한다.
Source 전체 dump에서 필드를 삭제하는 방식 대신 명시적 allowlist로 raw payload dict를 완성하고,
기존 `FeedPersonalizationContextV1.model_validate(payload, context={"service_timezone": ...})`를
최상위 경계에서 한 번 호출해 검증된 Context만 반환한다. Invalid row/count를 보정·drop하지 않는다.
Raw identity/ownership 필드는 최종 Context 구조에 포함하지 않지만 free text는 원문 그대로다.
Structural minimization은 완전한 PII 제거가 아니며 sanitizer/의료 해석/요약/attribution은 없다.
Summary를 파싱하거나 이전 ref를 재매핑하지 않는다. Persistent summary 생성/저장 계층에서도
Context-local ref를 영속 환자 identity로 사용해서는 안 된다.

실패는 `FeedContextLoadError.code` allowlist: `invalid_request`, `invalid_clock`, `source_load_failed`,
`invalid_source_snapshot`, `context_assembly_failed`, `context_validation_failed`다.
Source 호출 exception은 source_load_failed, 반환 DTO/identity 위반은 invalid_source_snapshot,
기존 최종 Contract 위반은 context_validation_failed로 구분한다. 외부 메시지는 고정 code만 담고
공개 args/code 및 우리 일반 로그에 Source/원본 exception/ValidationError input/UUID/text를 포함하지 않는다.
Loader 자체가 실패를 전파한다. Production Stage 1 adapter도 이를 전파하며 공통 Workflow는 context_prepare에서 중단한다.
`raise ... from None`은 모든 APM의 exception context/traceback locals 제거를 보장하지 않는다.
`reference_date`를 계산할 수 없는 service timezone도 기존 `context_validation_failed` 분류를
유지하며 잘못된 기준 날짜로 Source를 호출하지 않는다. Clock 오류를 이 분류와 혼합하지 않는다.

### V1.1 Backend boundary / projection / ordering

`UserProfileContext` / `PatientContext` / `get_user_profile_context()`는 current-state/demo aggregate다.
각각 `FeedContextSourceSnapshot` / `SourceManagedPatient` / `FeedPersonalizationContextV1`과 같은
계약이 아니다. Backend aggregate는 검사 종류별 최신 1건, current medication만, 최근 safety 중
flag가 true인 row만, 일부 최근 방문 및 예약을 선택한다. CareLog와 Long-term Summary는 없다.
자체 `today()` / `datetime.now(APP_TIMEZONE)`도 사용하므로 Source 인자를 추가하는 것만으로
Feed의 시간 계약을 충족하지 않는다. 이를 그대로 Source Snapshot으로 취급하거나 기존 Feed를
latest assessment/current medication/event-only safety로 축소하지 않는다.

향후 Production Source는 Backend 모델 전체 dump 후 삭제 대신 **명시적 allowlist projection**으로
필요한 필드만 새 Source 객체에 조립한다. 다음은 Adapter 정책이며 실제 Production Adapter는 없다.

| Backend DTO → Source DTO | 사용하는 필드 | 제외하는 필드 |
|---|---|---|
| `CaregiverProfileOut → SourceCaregiverProfile` | `user_id`, `relationship`, `burden_score`, `mood_score`, `lifestyle_tags`, `updated_at` | allowlist 밖 모든 필드 |
| `PatientOut → SourceManagedPatient` 기본 프로필 | `id → patient_id`, `caregiver_id`, `dementia_stage`, `diagnosis_date`, `symptoms`, `interests`, `updated_at` | `name`, `created_at` |
| `AssessmentOut → SourceClinicalAssessment` | `patient_id`, `assessment_type`, `score`, `result_detail`, `assessed_at` | `id`, `assessed_by`, `created_at` |
| `SafetyEventOut → SourceSafetyEvent` | `patient_id`, `event_date`, `has_fall`, `has_wandering`, `has_missing`, `note` | row `id`, `created_at` |
| `MedicationOut → SourceMedication` | `patient_id`, `drug_name`, `is_taking`, `start_date`, `end_date`, `note` | `id`, `dosage`, `frequency`, `created_at`, `updated_at` |
| `MedicalVisitOut → SourceMedicalVisit` V1 | `patient_id`, `visit_date`, `is_visited`, `department`, `visit_content` | `hospital_name`, row `id`, `created_at`, `updated_at`, 005 신규 상세 필드 |

Patient의 종속 collection/count는 별도 Feed Read 결과가 제공해야 한다. Backend float score를
현재 Source Decimal로 연결할 때 non-null 값에는 `Decimal(str(score))` 수준의 기계적 변환만 허용한다.
임상적 rounding/해석을 하지 않으며 float에서 잃은 정밀도를 복원한 것으로 간주하지 않는다.
향후 Feed 전용 Backend DTO가 Decimal을 직접 보존하는 것이 바람직하다.

현재 Backend 조회 코드로 확인된 Source stable ordering baseline:

| Collection | Ordering |
|---|---|
| Clinical Assessment | `assessed_at DESC → created_at DESC → id` |
| Safety Event | `event_date DESC` (환자/날짜별 UNIQUE) |
| Medication | `is_taking DESC → start_date DESC NULLS LAST → created_at DESC → id` |
| Medical Visit | `visit_date DESC → created_at DESC → id` |
| CareLog | `logged_at DESC → id` (정식 Backend Feed Read 구현 계약) |

`created_at` / raw DB `id`는 server-side tie-break에만 쓰며 final Context로 노출하지 않는다.
Medication ordering은 이 기준으로 확정하지만 **ordering != eligibility**다. Current-only selection을
Feed eligibility로 승격하지 않으며 Loader는 child/log 배열을 재정렬하지 않는다.

Collection별 temporal semantics는 Backend Feed Read Contract에서도 보존해야 한다.

| Collection | Temporal semantics |
|---|---|
| Clinical Assessment | future `assessed_at` 불가 |
| Safety Event | future `event_date` 불가; false flag row도 의미 보존 |
| Medication | future start/end 허용, 양쪽 값이 있으면 start ≤ end; `is_taking`/date conflict 자동 보정 금지 |
| Medical Visit | future reservation + false 허용; future + `is_visited=true` 불가 |
| CareLog | exact `[recent_period_start, recent_period_end)` |
| Long-term Summary | start ≤ end ≤ reference date; generated instant ≤ reference instant; end ≤ generated service date |

모든 collection에 동일한 date cutoff를 적용하지 않는다. 명시된 Source eligibility로 조회 전에
제외하는 것은 정상 selection이고 반환된 Contract-invalid row는 validation failure다.
Loader의 사후 invalid row 삭제는 금지하며 Source의 계약 밖 임의 filter도 Contract 위반이다.
**Source-level eligibility filter는 명시된 Feed Read Contract에 따라서만 적용하며,
반환된 record의 validation 실패를 숨기기 위한 사후 필터로 사용해서는 안 된다.**

### V1.1 Backend Feed Read success / failure / snapshot

성공한 Backend Feed Read는 대상 user가 존재하고 trusted user scope 접근이 허용되며,
모든 required read와 collection count가 완료되고 required single logical snapshot이 충족된
완전한 결과를 뜻한다. Loader의 `reference_time`, `reference_date`, recent period를 모두 사용하고
partial failure가 없어야 한다. 함수가 값을 반환했다는 사실만으로 성공을 선언하지 않는다.

이 성공 조건이 충족된 뒤에만 **Empty = successful absence**로 해석한다.

| 성공한 read 결과 | 의미 |
|---|---|
| `caregiver_profile=None` | user는 존재하고 profile 조회에 성공했지만 profile row 없음 |
| managed patients `items=[], total_count=0` | patient 조회 성공, eligible patient 없음 |
| recent CareLogs `items=[], total_count=0` | recent window 조회 성공, eligible log 없음 |
| `long_term_summary=None` | summary 조회 성공, eligible summary 없음 |

User 미존재, trusted scope 권한 실패, DB/query 실패, 필수 sub-read 일부 실패, count 실패,
snapshot 보장 실패, temporal selection 실패, required read path 미구현, schema/function 미준비,
일부 patient/collection만 성공하거나 기타 계약 수행이 불완전한 상태는 **read failure**다.
User 없음 → empty patients, CareLog 실패 → 0/0, Summary 미구현 → None, count 실패 →
`total_count=len(items)`, 일부 patient 실패 → 성공한 patient만 반환하는 변환을 금지한다.
Partial read를 정상 Snapshot으로 승격하지 않고 fail-fast한다.

정식 인터페이스는 아래 `get_feed_context_snapshot(...) -> FeedReadSnapshot`으로 확정하고
실제 함수/DTO는 Backend 담당자가 구현한다. Failure는 `FeedUserNotFound`/`FeedReadError`로 구분한다. 하나의
`get_feed_context_snapshot(...) -> None`이 user 없음/권한 실패/정상 empty/query failure를 동시에
표현하는 모호한 계약을 피한다. Backend DTO를 AI Source DTO와 동일하게 만들 필요는 없다.
이 문서에서 새 Backend DTO/function을 구현하지 않는다.

| Boundary | 기존 Loader error |
|---|---|
| Backend Feed Read failure → Production Source call failure | `source_load_failed` |
| Source DTO structure/type 또는 ownership/count/cross-ref 위반 | `invalid_source_snapshot` |
| Projection 수행 실패 | `context_assembly_failed` |
| Final Context temporal/semantic validation 실패 | `context_validation_failed` |

새 error code는 없다. Backend raw exception text/DB details/UUID/free text/ValidationError input을
공개 error나 일반 로그에 추가하지 않는다. `invalid_request`/`invalid_clock`도 기존 분류를 유지한다.

`total_count`는 **동일 logical snapshot**에서 authorization + 명시된 eligibility + temporal/base
filter를 통과한 eligible population 전체 수이며 inclusion/selection **이전** count다. Generic
`len(items)` fallback은 금지한다. 예를 들어 eligible 150건을 limit 100으로 반환했다고 total을
100으로 만들지 않는다. 현재 list helper의 pre-limit count 부재는 Production integration blocker다.
Managed patients는 `total_count == len(items)`로 eligible 전체 집합을 포함해야 한다.
`list_patients()`가 limit 없이 반환하는 것만으로 auth/count/snapshot 계약을 대체하지 않는다.

Single logical snapshot은 read/count가 같은 일관된 DB 상태를 관찰하는 보장이다.
`reference_time`의 instant 기준 및 `reference_date`의 service calendar 기준과 구분하며,
mutable profile의 historical as-of reconstruction을 요구하지 않는다. `get_caregiver_profile()`,
`list_patients()`, `list_assessments()`, `list_safety_events()`, `list_medications()`,
`list_medical_visits()`를 단순 순차 호출하는 것으로 충족했다고 간주하지 않는다.
PostgreSQL `READ COMMITTED`에서는 같은 transaction도 statement별 snapshot이 다를 수 있다.
Backend/DB가 composite Feed Read Contract 또는 동등한 consistent snapshot boundary를 제공해야
하며 isolation/transaction 방식도 그 계층 책임이다. AI가 `app/db/connection.py`를 직접 사용하거나
raw SQL/transaction으로 우회하지 않는다.

### V1.1 provenance / canonical metadata / integration status

CareLog는 Backend 데모 aggregate에 없어도 V1에서 유지한다. 향후 read는 trusted user scope,
exact recent window, 현재 managed patient set의 eligible logs와 정책상 허용된 `patient_id=None`
records, stable ordering 및 pre-limit total을 제공해야 한다. Null은 caregiver self-care를 뜻하지
않으며 `ON DELETE SET NULL`로 삭제 환자와 과거 연결된 row일 수 있다. 삭제 환자 retention/
eligibility는 미확정이다. Summary도 계속 user-scoped derived personalization context이며
patient clinical memory가 아니다. Read 계약은 user scope, reference 기반 eligibility, latest eligible
row deterministic selection과 `period_start`, `period_end`, `summary`, `updated_tags`, `generated_at`을
제공해야 한다. 조회 기능 부재/실패를 None으로 숨기지 않는다.

Migration 005 이후 정상 신규 write의 `visit_content`는 **기타 자유 메모**다. 기존 값은 structured
column으로 backfill되지 않아 legacy row에서는 과거 전체 진료 내용 원문일 수 있다. Source/Loader는
원문 untrusted data를 보존하고 재분류/임의 parsing/추정 이동하지 않는다. 신규 `visit_reason`,
`diagnosis`, `treatment_content`, `test_summary`, `medication_change`, `doctor_note`, `follow_up_plan`은
SourceMedicalVisit/MedicalVisitContext V1에 추가하지 않고 `visit_content`에 concat하거나 일부만
임의 선택하지 않는다. Legacy 원문을 이들 field로 추론 분해하지 않는다. 향후 Context revision에서
privacy/token budget/provenance와 함께 포함 범위를 검토한다.

Backend 정상 write path의 canonical provenance는 burden score = Zarit 계열 0–88,
mood score = PHQ-9 0–27이며 stage vocabulary는 `경도인지장애`, `경도`, `중등도`, `중증`이다.
이를 Context의 strict int/null 범위 validator나 stage Literal로 강화하지 않는다. Loader는 unknown/
legacy 저장값을 재분류·삭제하지 않고 severity/cutoff/진단을 계산하지 않는다. Context V1의
defensive raw-value 경계는 그대로다.
Assessment metadata 정본은 `app.user_schemas.ASSESSMENT_TYPES` / `list_assessment_types()`이며
canonical type, min/max/step/allowed/higher_is_worse를 제공한다. 이를 각 사용자 Context에 복제하거나
새 필드로 넣지 않고 unknown/legacy assessment type도 새 validator로 거부하지 않는다.
Feed 실행에서의 활용 방식은 후속 Prompt Policy다.

책임 흐름은 `Trusted AgentContext.user_id → ProductionFeedContextSource → Backend internal Feed
Read Contract → DB access layer`다. 데모 `/feed*` HTTP API를 AI에서 다시 호출하는 구조를 기본으로
채택하지 않으며 앱 API authentication과 내부 trusted identity boundary를 혼합하지 않는다.
실제 Backend 입력은 `user_id`, `reference_time`, `reference_date`, `recent_period_start`,
`recent_period_end`이며 `app.db.feed_read.get_feed_context_snapshot`과 `app.feed_schemas.FeedReadSnapshot`이 정본이다.

**ProductionFeedContextSource 구현 및 Stage 1 오프라인 연결 완료.** 실제 Snapshot 조회 함수를 DI로
호출하고 real CareLog/Summary와 pre-cap count를 기존 Source→Loader→Prepare 경로로 전달한다.
Development aggregate Source와 UNREAD 자리표시자는 제거했다. 기존 API Demo 호환 경계는 별도다.
실 DB 통합 실행 및 HTTP 활성화는 완료를 주장하지 않는다. 진료 상태/중단 약물의 SELECT·COUNT
정책 불일치는 아래 Backend 수정 필요 항목이며 Adapter의 fail-fast만으로 해결되었다고 보지 않는다.
삭제 환자 Summary invalidation/regeneration, future patient/child limits, latest Summary selection
algorithm, token/input budget, visit detail 확장, free-text privacy/redaction, cache/retry/degraded mode,
Production APM privacy 및 HTTP wiring은 후속이다. Timezone binding/reference_date 전달,
Medication stable ordering, aggregate와 Source 구분, empty와 read failure 의미는 V1.1에서 확정했다.

### Production Feed Context Source — 실제 Backend Snapshot / Stage 1 연결

`workflows/feed_context_backend_source.py`의 `ProductionFeedContextSource(read_snapshot=...)`는
실제 `app.db.feed_read.get_feed_context_snapshot`을 주입받고 `app.feed_schemas.FeedReadSnapshot`을
기존 Source DTO로 변환한다. 새로운 Backend DTO·함수·범용 Factory를 만들지 않는다.
사용처가 전용 테스트뿐이던 DevelopmentFeedContextSource는 제거했다. 소속·시간·비변조·
개인정보·전체 환자·실패 전파의 유효한 테스트 의미는 정식 Snapshot/Loader 테스트로 이전했다.

**완료 범위.** Source→DefaultFeedContextLoader→FeedPersonalizationContextV1→
prepare_production_feed_input→FeedWorkflow의 내부 연결을 오프라인 테스트로 검증한다.
실제 Backend 함수 본문도 실행하되 DB I/O만 Mock으로 대체한 테스트가 있다.
이는 **실 DB 통합 검증 또는 실제 HTTP 경로 활성화 완료가 아니다.** 기존 `/recommendations`의
단일 환자 Demo 계약과 `get_feed_execution()`은 변경하지 않는다.

Production 조립은 호출부에서 clock을 명시적으로 주입한다 (clock은 aware datetime 반환):

```python
from app.db.feed_read import get_feed_context_snapshot
from app.user_schemas import APP_TIMEZONE
from app.ai.agent.workflows.feed_context_backend_source import ProductionFeedContextSource
from app.ai.agent.workflows.feed_context_loader import DefaultFeedContextLoader
from app.ai.agent.workflows.feed_context_prepare import prepare_production_feed_input

production_source = ProductionFeedContextSource(read_snapshot=get_feed_context_snapshot)
production_loader = DefaultFeedContextLoader(
    source=production_source, clock=clock, service_timezone=APP_TIMEZONE,
)

def production_context_prepare(context):
    return prepare_production_feed_input(context, production_loader)

# FeedWorkflow(context_prepare=production_context_prepare,
#              feed_execution=feed_execution, backend_handoff=backend_handoff)
```

Source는 Loader의 `user_id`, `reference_time`, `reference_date`, `recent_period_start`,
`recent_period_end` 다섯 키워드 값을 **그대로 한 번** 전달한다. 독립 clock/SQL/추가 조회/
get_user_profile_context fallback은 없다. AgentContext.patient_id로 환자를 필터링하지 않는다.

**투영과 검증.** 실제 Backend 모델을 Python 값으로 dump 후 strict 재검증하여 중첩 변조·누락·
타입 불일치를 거부한다. 이 dump는 형식 검증용이며 Source 생성자에 통째로 전달하지 않는다.
각 Source 필드만 명시적으로 투영하고 Decimal을 float로 바꾸지 않는다. 원본 items 순서와
`total_count`를 보존하며 count를 len(items)로 재계산하거나 정렬·LIMIT·삭제·합성을 하지 않는다.
관리 환자는 total_count==len(items), 최대 20명이며 기존 Loader invariant로 소속/중복/참조를 검사한다.
실제 CareLog items/count와 월간 요약을 적재한다. 정상 조회의 None/빈 결과만 그대로 유지하며
개발용 자리표시자는 사용하지 않는다. CareLog NULL patient_id는 NULL 그대로 옮기고 Loader가
patient_ref=None으로 변환한다. 본인 기록 또는 특정 환자 기록이라고 임의 추정하지 않는다.

Backend 복약 `dosage`/`frequency`와 진료 `visit_reason`/`diagnosis`/`treatment_content`/
`test_summary`/`medication_change`/`doctor_note`/`follow_up_plan`은 제외한다.
`visit_content`는 기타 자유 메모로 직접 보존하며 진단·처치 정보로 재해석하지 않는다.
UUID→patient_ref와 Coverage/최종 기간 검증은 기존 Loader, LLM allowlist는 기존 Mapper 책임이다.
CareLog·요약이 full Context에 있다는 사실은 Provider 전송을 의미하지 않는다. UUID, 원점수,
CareLog/월간 요약/진료·복약 원문/coverage는 기존 Provider Input에서 계속 제외한다.
증상·관심사 등 허용 자유 문자열의 PII 비식별화는 별도 후속 작업이다.

**실패 정책.** 실제 Backend 예외 `FeedUserNotFound(LookupError)`, `FeedReadError(RuntimeError)`,
`FeedPatientLimitExceeded(FeedReadError)`는 같은 예외 인스턴스/종류로 전파한다.
Adapter 자체 변환/검증 오류만 `ProductionFeedContextSourceError`의 고정 메시지로 감싸며,
Backend 원문·환자 데이터·ValidationError 입력을 새 오류 문자열에 넣지 않는다.
Loader는 기존 source_load_failed, Workflow는 context_prepare 단계 실패를 노출하고 이후
feed_execution/handoff를 중단한다. suppressed chaining은 APM traceback locals 제거 보장이 아니다.
Adapter 내부 fallback은 없다. 추천 API의 별도 검색 fallback과 이 책임을 혼동하지 않는다.

**Backend 수정 필요 — 진료/복약 SELECT와 COUNT 정책 불일치.**

- `_VISIT_WHERE`는 날짜·환자만 검사한다. 반환된 미래 완료 진료 또는 과거 미방문 진료는
  Production Source가 전체 실패시킨다. 당일 미방문 예약은 임의 금지하지 않는다.
  Backend는 과거 완료/미래 예약의 상태 조건을 SELECT와 COUNT에 똑같이 적용해야 한다.
  당일 예약 정책은 별도 합의가 필요하다. 기존 예약 우선 최대 5건/총 20건 배분은 Backend에 남긴다.
- `_MEDICATION_WHERE`는 중단 약물 end_date의 상한이 없다. `is_taking=False`이면서
  `end_date>reference_date`인 반환 기록은 Source가 실패시킨다. Backend는 중단 약물을
  `reference_date-90일 <= end_date <= reference_date`로 SELECT/COUNT 모두 수정해야 한다.
  현재 복용 약물은 별도로 포함하며 저장된 flag/date 모순을 임상적으로 보정하지 않는다.

Source fail-fast는 SQL 수정의 대체가 아니다. 잘못된 기록이 상한 자리를 차지해 밀려난 정상 기록,
또는 count에만 들어간 오류는 Adapter가 복원/판별할 수 없다. record 삭제/count 감소/추가 조회로
정상 Snapshot을 가장하지 않는다. Backend SQL/DTO는 이번 Agent 작업에서 수정하지 않는다.
또한 호출부 인증은 Source 소속 일치 검사와 별개다.

오프라인 검증 (`backend/`):

```bash
MINDCARE_RUN_PROVIDER_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest app.ai.agent.tests.test_feed_context_backend_source app.ai.agent.tests.test_feed_context_production_source app.ai.agent.tests.test_feed_context_loader app.ai.agent.tests.test_feed_context_prepare app.ai.agent.tests.test_feed_workflow app.ai.agent.tests.test_feed_execution
```

실 DB `tests.test_feed_read`와 실제 Source/Stage 1 테스트는 **001~006 적용된 로컬 테스트 DB**에서만
실행한다. 현재 확인한 로컬 개발 DB에는 006의 `profile_summaries_user_period_key` 및
`idx_profile_summaries_user_period` 인덱스가 없어 이번 실 DB 통합 테스트는 미실행이다.
테스트를 위해 migration/샘플 시드를 자동 실행하거나 운영·공용 DB를 변경하지 않는다.

### 외부 recommendations API 임시 호환 경계 — 제거 대기

현재 `backend/app/api/recommendations.py`는 기존 `prepare_demo_feed_input()`을 import하고
UserProfileContext와 단일 환자 선택으로 호출한다. 해당 Backend/API 파일은 이번 작업에서 변경하지 않는다.
API import를 깨뜨리지 않도록 feed_context_prepare.py의 동일 이름·인자 shim을 남겼다.
shim을 호출할 때만 feed_demo_compat.py를 지연 import한다. 정식 Source/Loader/Prepare 및 실행
Fixture는 이 모듈·Demo DTO·선정 규칙을 사용하지 않는다. Production 실패 시 fallback도 없다.

feed_profile_planning.py의 기존 Demo Mapper/DTO/선정 기능은 API 호환에 필요한 범위에서
feed_demo_compat.py로 옮겼다. 새로운 Context/Source/Planning V2 모델을 만들지 않았다.
공통 allowlist serializer와 오류 클래스는 정식 Prepare에만 있다. 호환 함수만 기존
single_patient_demo scope와 0~1명 선정 정책을 유지하며 기존 API 출력·실패 의미를 보존한다.
`PROFILE_PLANNING_AVAILABLE_SOURCES`와 Development Source/전용 정렬·건수·자리표시자 로직은 제거했다.

**상태: Agent 내부 정식 경로 통합 완료, 외부 API 호환 경계 제거 대기.** API 담당자가
get_feed_context_snapshot → Production Source → Loader → Prepare와 caregiver_all 계약으로
호출부를 전환하고 기존 import/call이 없어지면 shim·feed_demo_compat.py 및 전용 호환 테스트를 제거한다.
API 활성화·인증·응답/환자 선택 계약 전환은 Backend 담당자의 후속 작업이다.

#### 정식 Feed Read Contract/Policy — 실제 Backend 구현 및 남은 조건

Backend/DB 담당자가 다음 조회 함수를 `app/db/feed_read.py`에, 반환 DTO를
`app/feed_schemas.py`에 구현했다. Production Source는 실제 DTO와 함수를 DI로 사용한다.
위 개발용 DI는 별도로 계속 `get_user_profile_context(user_id)`를 사용한다.
이 절은 기존 V1.1 문서에서 미확정으로 남긴 기록별 조회 범위·포함 상한·CareLog tie-break를
구체화한다. 기존 DTO/Protocol/Loader의 런타임 구조와 검증 정책은 그대로 유지한다.

```text
get_feed_context_snapshot(
    *, user_id, reference_time, reference_date,
    recent_period_start, recent_period_end,
) -> FeedReadSnapshot
```

| 계층 | 책임 |
|---|---|
| Backend Feed Read | DB/SQL, 사용자 존재·전체 관리 환자 조회, 기록 eligibility/선정/LIMIT/count, 기준 시각과 안정 정렬, 조회/빈 결과 구분 |
| Agent Adapter | 조회 함수 호출, 명시적 필드 투영, Source Snapshot 조립, 필수 데이터·형식·소속/중복/참조 검증, 실패 전파 |
| DefaultFeedContextLoader | Source 재검증, UUID → patient_ref, Coverage, V1 Context 조립·최종 검증 |
| Provider Input Mapper | 기존 allowlist 최소화, UUID·불필요한 원문 제외, caregiver_all 유지 |

Backend는 **읽기 전용 단일 REPEATABLE READ 트랜잭션**에서 조회와 count가 동일한 DB Snapshot을
보도록 보장한다. 관리 환자는 0명도 정상이며 1~20명은 전원 반환, 21명 이상은 실패한다.
Adapter는 SQL을 실행하거나 기간/LIMIT으로 재선정하지 않고, 잘린 목록을 복원하거나 가상
레코드를 추가하지 않는다. Snapshot 일관성은 과거 mutable profile의 DB AS-OF 복원을 뜻하지 않는다.

**Eligibility(E)와 inclusion(I).** E는 기간·상태·user/patient Scope 조건을 모두 만족하는 조회 대상,
I는 E에 기록별 포함 상한과 선정 정책을 적용하여 실제 반환한 집합이다.
`total_count = |E|`는 **상한 적용 전 건수**이며 `len(items) = |I|`다.
기존 Loader가 `included_count=len(items)`, `is_truncated=(total_count > included_count)`를 계산한다.
평가 37건 중 20건 반환이면 `total_count=37`이며 정상적인 자식 기록 truncation이다.
자식 기록은 상한 초과 자체로 실패하지 않지만, 관리 환자 목록은 전체 포함이 필수다.
개발용 `total_count=len(items)`는 이미 선택된 현재 상태 목록의 임시 값이고 정식 E의 count가 아니다.
현재 Adapter에서 별도 count 조회를 추가하거나 이 값으로 실제 이력 건수를 추측하지 않는다.

| 기록 | 정식 Backend의 조회 대상 E | 독립적인 포함 상한 |
|---|---|---|
| 임상 평가 | reference_date 기준 최근 365일 | 환자당 20건 |
| 안전 기록 | 최근 180일 중 낙상·배회·실종 플래그가 하나 이상 참인 날 | 환자당 50건 |
| 복약 기록 | 현재 복용 중이거나 최근 90일 이내 중단된 약 | 환자당 30건 |
| 병원 방문 | 과거 365일 완료 진료 및 향후 90일 예약 | 환자당 20건 |
| CareLog | 전달받은 정확한 최근 30×24시간 구간 | 간병인당 200건 |
| 월간 개인화 요약 | 기준 날짜 전에 종료되고 기준 시각까지 생성된 요약 중 최신 1건 | 1건 |

기준은 전달받은 `reference_time`과 서비스 시간대의 `reference_date`이며 Adapter에서 새 now/today를
호출하지 않는다. **이 기간·상한을 현재 개발용 aggregate에 억지로 적용하지 않는다.** 날짜 기반
기간의 DATE 범위는 Backend 구현의 inclusive `reference_date-window_days`~`reference_date`를
따른다. 요약은 `period_end DESC, generated_at DESC, id`로 최신 1건을 선택한다.

**선정과 최종 정렬은 별개다.** Backend의 안정 정렬 기준:

- 평가: `assessed_at DESC, created_at DESC, id`
- 안전: `event_date DESC` (환자/날짜당 한 행)
- 복약: `is_taking DESC, start_date DESC NULLS LAST, created_at DESC, id`
- 진료: `visit_date DESC, created_at DESC, id`
- CareLog: `logged_at DESC, id`

Backend는 미래 예약에 가까운 순으로 최대 5건을 먼저 배정하고 남은 자리를 최근 지난 진료로
채우며, 한쪽 부족 시 다른 쪽에서 총 20건까지 채운다. 상태 eligibility 수정은 위 Backend TODO다.
Agent는 선정된 순서의 의미/실제 값을
바꾸지 않고 투영한다. 기존 개발용 최소 정렬은 유지하되 최종 Backend 선정과 동일하다고 주장하지
않는다. 정렬용 id/created_at을 Agent Source DTO에 새로 추가하지 않는다.

**CareLog.** `[recent_period_start, recent_period_end)` 반열린 구간을 정확히 사용한다.
`recent_period_end == reference_time`, 길이는 정확히 30×24시간이며 최대 200건을 포함한다.
`user_id`는 요청 간병인과 일치하고, non-null `patient_id`는 현재 관리 환자 범위에 속해야 한다.
NULL도 포함 가능하며 본인 기록/특정 환자 기록이라고 추정하지 않는다. Loader는 NULL을
`patient_ref=None`으로 보존한다. 정식 total_count는 이 조회 대상의 실제 건수다.
현재 빈 collection/0건은 **미조회 개발용 자리표시자**이며 실제 CareLog 0건을 뜻하지 않는다.

**월간 요약.** 요청 간병인의 유효한 기간을 가진 요약 중 `period_end < reference_date`,
`generated_at <= reference_time`인 최신 1건을 Backend가 선정한다. 실제로 대상이 없을 때만 None이다.
기존 Context 날짜/기간 validator는 그대로 유지한다. 정식 Source는 Backend가 반환한 실제 None만 보존한다.

**필드 정책.** 실제 Backend FeedRead* DTO의 클래스/추가 필드가 달라도 Source 허용 필드만 투영한다.
`visit_reason`, `diagnosis`, `treatment_content`, `test_summary`, `medication_change`, `doctor_note`,
`follow_up_plan`, `dosage`, `frequency`는 제외한다. `visit_content`는 005 이후 기타 자유 메모이며
진단/처치로 재해석하거나 005 이전 원문을 변환·재분류하지 않는다. DB NULL 배열을 Backend가
유효한 []로 정규화하는 것은 허용하지만, 필수 조회 필드 누락·조회 실패를 []로 보정하지 않는다.
추가 신호 활용 및 자유 서술 PII 처리는 별도 작업이다.

**오류 계약.** Backend는 없는 사용자에 `FeedUserNotFound`, 조회·건수·검증 실패에 `FeedReadError`를
발생시킨다. Adapter는 빈 Snapshot으로 바꾸지 않고 기존 Loader의 `source_load_failed` 래핑을 유지한다.
존재하는 사용자의 환자 0명, 미등록 caregiver profile, 대상 기록 0건은 모든 필수 조회 성공 후의 정상
부재다. 없는 사용자·Backend 실패·누락된 필수 데이터는 실패다. 새 외부 오류 체계는 만들지 않는다.

**Backend 구현 후 테스트 TODO:** 확정 인자 전부의 정확한 전달, 실제 FeedReadSnapshot → Source
필드 투영, pre-cap eligible count와 상한/Coverage, CareLog 30×24시간 경계·NULL ref, 요약 기준일/
생성 시각/선정, 단일 DB Snapshot 및 실패 처리, 완료 진료·예약 배분, 정상 DB 데이터 통합 검증.
Production Source의 실제 DTO/함수 연결과 인자/건수/기간/정렬/참조 검증은 오프라인 테스트로
확인한다. 실 DB Snapshot 일관성·정상 DB 데이터 및 SQL 상태 조건 수정 검증은 아직 필요하다.
Development 자리표시자는 제거했으며 정식 Source에는 사용하지 않는다.
기존 3-Stage/Loader/Mapper 역할을 유지하며 인증·개인정보 정책 및 정식 API 연결은 후속이다.

### Production Stage 1 — 통합 Contract/Policy

정식 경로는 실제 FeedReadSnapshot → Source Snapshot → Loader → FeedPersonalizationContextV1 →
Provider 최소 JSON이다. UserProfileContext/current-state aggregate는 정식 Source 대용이 아니다.
소속·중복·전체 관리 환자 검증은 기존 Source/Loader를 재사용하고, 날짜·시간과 Coverage는 기존
Context validator에 맡긴다. 새로운 DTO/Mapper 계층이나 중복 검증 엔진을 만들지 않는다.

| 계층 | 책임 |
|---|---|
| ProductionFeedContextSource | Backend 요청 1회, 5개 인자 그대로, 명시적 투영, count/순서/Decimal/실제 CareLog·요약 보존, 방어적 반환 정책 검증 |
| DefaultFeedContextLoader | 소속/중복/완전성, UUID bytes 기반 patient_ref, 환자별 신호 분리, 기간/시간, 기존 V1과 Coverage |
| feed_context_prepare.py | 전체 V1 재검증, allowlist 최소 투영·JSON 직렬화, 고정 오류 메시지; Loader 오류 종류/코드 보존 |
| FeedWorkflow / FeedExecutionV1 | 기존 3단계 및 실패 중단, 기존 Runner·검색 Tool·출력/출처/Handoff 계약 |

**보존한 Demo의 유효한 의미:** 소속·중복·신호 분리·시간 정합성·원본 비변조·최소 공개·
임상 과잉 해석 금지·Cold Start·고정 오류·후속 Stage 중단이다. 이미 Source/Loader/Context에
있는 규칙을 복제하지 않는다. 실제 안전 이벤트의 사건 플래그가 모두 false인 반환 기록은
Backend의 event-only eligibility에 맞춰 Source에서 전체 실패한다. 레코드 삭제/count 보정은 없다.
이는 Source/Context DTO 필드나 validator 변경이 아니며 count-only 오류는 Backend 수정이 필요하다.

**정식 경로에서 폐기한 Demo 규칙:** single_patient_demo, 0~1명 선정, patient_id 필터,
UserProfileContext 중심 구성, 평가 종류별 latest 1건/이력 중복 금지, 현재 복약만 허용,
recent/upcoming 방문 목록 분리, 자체 정렬·선정, aggregate generated_at 기준,
미조회 CareLog/요약 자리표시자, total_count=len(items) 대체 계산이다.
같은 assessment_type의 여러 시점 기록과 최근 중단 약물은 정식 경로에서 정상 보존한다.
정식 DTO에 없는 record ID의 유일성은 Backend 책임이며 가상의 ID를 만들거나 이력을 dedup하지 않는다.

**Coverage:** `I ⊆ E`, `total_count=|E|`, `included_count=|I|`, `is_truncated=(|I|<|E|)`.
Backend가 컬렉션별 E/I·상한·순서를 결정한다. Source는 받은 items 순서와 total_count를 유지하고
Loader는 환자만 UUID bytes 순으로 정렬하여 ref를 만들며 child/log 순서는 바꾸지 않는다.
기간이나 count를 임의 재계산하지 않는다. 부족한 read를 정상 빈 결과로 위장하지 않는다.

**시간·임상 해석:** Loader의 reference_time/reference_date가 유일한 기준이다.
동일한 Backend logical Snapshot은 historical AS-OF 조회가 아니다. 저장된 profile/assessment/
복약 상태에서 현재 진단·단계·중증도·약효를 추론하지 않고 codebook/검사점수의 과잉 해석을 금지한다.
현재 복약 flag와 과거 종료일의 모순을 임상적으로 보정하지 않는다. Event 없음/미공개 데이터는
안전함·임상적 부재의 증거가 아니다. CareLog·월간 요약은 전체 Context에 보존하지만 Provider에는 제외한다.

**테스트 자산 이전:** 기존 Production Adapter 29개와 Loader·Context 테스트는 유지한다.
FeedWorkflow/Execution/Prompt privacy의 Runner·Tool·출처·Handoff 테스트는 삭제하지 않고
정식 Snapshot/Loader Fixture로 전환했다. 기존 Demo/Development 전용 61개 테스트는 아래처럼
유효한 의미를 이전·중복 통합하며 API 호환성 테스트 4개만 격리하여 유지한다.

| 기존 테스트 의미 | 정식 검증 위치 / 처리 |
|---|---|
| caregiver/patient/child ownership·중복·부분 반환 금지·누락/변조 | 기존 test_feed_context_production_source / test_feed_context_loader, 신규 Prepare 재검증·다중 환자 실패 검사 |
| 최소 공개·raw identity/상세 제외·비변조·free text·고정 오류 | test_feed_context_prepare, test_feed_prompt_privacy, 기존 Production 29개 |
| Cold Start·전체 20명·21명 실패·환자별 child provenance·범위 무시 | 기존 Production 29개, test_feed_context_backend_source / test_feed_workflow |
| Decimal/null·실제 count/Coverage·동일 평가 이력·중단 약물·반환 순서 | 기존 Production 29개 및 이전한 Backend Source 테스트; Demo latest/current/자체 sort 규칙을 복사하지 않음 |
| 시간·기준 시각·safe errors·Stage 실패·기존 Runner/Tool/Handoff | 기존 Loader / Production 29개, Prepare / Workflow / Execution 회귀 테스트 |
| UNREAD placeholder·aggregate selected-count·record-ID dedup·Demo 전용 선정/정렬 | 정식 테스트에서 제거/대체; Backend DTO에 없는 ID는 생성하지 않음 |
| 기존 외부 API의 0~1명 선정/import/call | test_feed_demo_api_compat 4개; 정식 Stage 1의 계약으로 간주하지 않음 |

전체 테스트 수의 감소는 이 중복 통합과 폐기된 Demo 규칙에 따른 것이다. 테스트 수 자체가 아니라
위 계약 검증의 보존, Production 출력 동등성 및 회귀 결과를 기준으로 판단한다.

### FeedExecutionV1 — 준비된 입력으로 LLM 반복 검색

정식 개인화 입력은 FeedPersonalizationContextV1이다. 공개 projection/serialization은 Stage 1의
feed_context_prepare.py가 소유한다. Prepare는 전체 V1을 재검증하고 기존 patient_ref와 공개 필드를
유지하여 caregiver_all JSON을 만든다. 새 중간 DTO/Planning Contract는 없다.

| 포함 | 제외 |
|---|---|
| caregiver relationship/lifestyle_tags | burden/mood score, updated_at, server identity |
| 환자별 patient_ref/dementia_stage/symptoms/interests | diagnosis_date/reference_time 및 모든 날짜·시각 |
| true safety flags의 fall/wandering/missing 목록 | safety note 및 event date |
| assessment 이력의 assessment_type만 | assessment score/result_detail/assessed_at |
| caregiver가 없으면 null, 환자가 없으면 [] | medication 전체, visit department/content 및 기타 narrative |

Positive flags는 제공된 selected rows에서만 고르며 false/row 부재를 안전함이나 임상적 부재로
해석하지 않는다. 여러 환자를 합성 환자로 병합하지 않는다. Projection은 자유 텍스트를
sanitize하거나 완전한 비식별화를 보장하지 않는다. 실제 CareLog/Summary는 전체 Context에
보존하며 현재 Provider 공개 정책에 따라 입력에서 제외한다. 미공개를 정상 empty로 해석하지 않는다.

**Runner/Policy.** FeedExecutionV1이 FeedPromptBuilder와 search_evidence 전용 Registry,
3/3 budget을 소유·검증한다. Executor/Runner Registry 불일치도 Provider 호출 전에 거부한다.
공용 Runner 기본값 5/10 및 Chat은 그대로다. 세 번째 Tool result 이후 final은 허용하고,
네 번째 call 또는 초과 batch는 실행하지 않는다. 한 turn의 여러 call과 실패한 실행도 budget을 쓴다.
LLM이 일반화된 query를 만들고 실제 Evidence를 본 뒤 충분하면 중단하거나 후속 검색한다.
정적 Topic Catalog/fixed query/alias matching/priority/greedy selection 및 별도 Plan DTO는 없다.

**Structured Output.** Wrapper는 공용 `run_structured(..., output_model=FeedAnswerV1)` 결과를
변환 없이 반환한다. Runner의 validate_final_output/evidence reference resolution을 그대로 사용하며
없는 call/index는 거부한다. 서버 provenance는 successful search Tool Call → EvidencePackage의
0-based evidence_index → FeedAnswerV1.source_ref → ResolvedEvidence다. Query가 특정 profile
signal에서 나왔는지, personal_reason의 의미적 정확성까지 증명하지 않는다.

**Failure.** Wrapper는 Runner의 limit_reached/provider_error/invalid_response/failed status를
예외로 바꾸지 않는다. Workflow가 completed Feed 결과만 Handoff하도록 차단한다.
입력 준비 실패는 고정 FeedProfileMappingError, 설정/실행 경계 exception은 고정 FeedExecutionError다.
Profile/query/evidence 원문 로그는 추가하지 않는다.

**미연결 경계.** 실제 Production Source/Loader/Prepare와 오프라인 Snapshot부터 V1 Handoff까지
기존 컴포넌트 연결을 검증했다. 외부 recommendations API는 임시 Demo 호환 경계를 사용한다.
actual RAG/production registry, Query Guard, top_k=5 server enforcement, 실제 Backend DTO 변환/저장,
same-PMID dedup, semantic duplicate query tracking, Feed history/novelty는 후속이다.
현재 recommendations API는 patient_id/단일 query 기반 별도 경로다. 복수 환자 FeedAnswerV1을
그 계약에 임의로 변환하거나 PMID를 DB paper_id로 꾸미지 않는다. 실환자 production 승인을 뜻하지 않는다.

오프라인 실행 검증 (`backend/`):

```bash
MINDCARE_RUN_PROVIDER_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest app.ai.agent.tests.test_feed_execution -v
```

**전체 prompt/query privacy.** `FeedPromptBuilder`를 공용 `AgentLoopRunner`에 주입한다.
FeedExecutionV1은 construction과 실행 직전에 이 builder를 확인하여 DefaultPromptBuilder의
raw `AgentContext` 직렬화를 차단한다. Identity는 서버의 auth/tool/persistence 경계에서 유지하고
Provider initial messages에는 request/user/patient ID를 직렬화하지 않는다. 공용 Runner 및 Chat의
DefaultPromptBuilder는 변경하지 않았다.
Caller는 이미 검증·최소화된 planning/Context payload만 user input으로 공급해야 한다.
전체 system/user/tool/evidence messages는 privacy 검토 대상이며 builder가 자유 텍스트·검색 근거·
LLM output·custom system prompt를 PII sanitize하는 것은 아니다.

고정 exact-query allowlist는 제거했다. LLM이 생성한 query는 현재 기존 EvidenceSearchInput 계약만
검증한다. 향후 실제 RAG Handler 호출 전에 Feed-specific query admission과 `top_k=5` 강제를
binding해야 한다. 현재 test-only registry의 validation seam은 합성 hook이며 production Query Guard가 아니다.
실제 Production registry/Feed execution wiring은 미연결이다. 회귀 테스트는 Fake Provider의 모든 turn에서
server identity가 제외되고 허용된 최소 research projection만 전달됨을 확인한다.
Allowlist projection은 field minimization뿐이다. Symptoms/interests/note/visit_content/CareLog/Summary
원문 속 PII sanitizer는 **미구현**이며 완전한 PII-safe 보장을 주장하지 않는다.

CareLog NULL 보존·non-null 현재 관리 환자 범위 검사는 위 계약을 따른다.
Deleted-patient CareLog eligibility는 Backend의 현재 관리 환자/NULL 정책을 따른다.
삭제 환자 유래 요약 retention/invalidation, Backend 진료·복약 SELECT/COUNT 정책 보강,
실 DB Snapshot 검증·HTTP 활성화·인증·free-text privacy·token budget·운영 APM·
cache/retry/degraded mode와 실제 RAG/Query Guard/Workflow persistence는 후속이다.

**정식 Stage 1 내부 연결 완료:** Backend 함수·DTO → Production Source → Loader/실제 Coverage →
full V1 → Prepare → FeedWorkflow를 오프라인으로 검증한다. DB I/O Mock 검증을 실 DB 운영 또는
HTTP 서비스 활성화 완료로 표현하지 않는다. 외부 API Demo 호환 경계 제거는 별도 담당자 전환을 기다린다.

```bash
MINDCARE_RUN_PROVIDER_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest app.ai.agent.tests.test_feed_context_loader -v
```

## 실행

`backend/`에서 기존 `requirements.txt` 의존성을 설치한 Python 환경으로 실행한다.
새 dependency는 없다. `unittest`는 Python 표준 라이브러리다.

```bash
.venv/bin/python -m unittest discover -s app/ai/agent/tests -t . -v
.venv/bin/python -m app.ai.agent.testing.demo
```

Windows 등에서는 활성화한 가상환경의 `python`으로 `.venv/bin/python`을 대체한다.
기본 테스트/데모는 API key, DB, 외부 네트워크가 필요 없다.
Provider 테스트는 설치된 `openai==1.51.0` + `httpx.MockTransport`로 API 형식만 검증한다.

선택적인 실제 Provider 검증은 기존 `app.config.settings.openai_api_key` binding과 명시적인
모델 이름을 준비한 뒤 실행한다. API key를 코드/명령줄에 넣거나 출력하지 않는다.

```bash
MINDCARE_RUN_PROVIDER_INTEGRATION=1 MINDCARE_AGENT_MODEL=<사용할_모델> \
  .venv/bin/python -m unittest app.ai.agent.tests.test_provider_integration -v
```

실제 테스트는 opt-in 없으면 skip이며, opt-in하더라도 key/model이 없으면 skip한다.
외부 연결 테스트를 실행하지 않았으면 실제 Provider 동작 검증으로 보고하지 않는다.

`OpenAIClient(sdk, model=model, timeout_seconds=20.0)`로 네트워크 timeout을 명시한다.
Adapter는 매 호출에 `max_retries=0`을 적용하고 네트워크 timeout을 남은 Agent deadline
이하로 제한한다. 주입한 SDK의 key/model/HTTP client 및 원래 설정은 변경하지 않는다.
HTTP timeout은 네트워크 단계별 제한이며 실행 중인 thread의 강제 취소를 보장하지 않는다.

## 파일별 역할

모든 Agent 코드/문서는 `backend/app/ai/` 내부에 있다. DB/RAG 공용 계약, main/config,
의존성 선언에는 변경이 없다.

| 파일 (agent/ 기준) | 역할 |
|---|---|
| `schemas.py` | Context, ToolCall, ModelTurn, Message, State, Result, 오류/메타데이터 trace |
| `outputs.py` | 최종 Chat/Feed V1, Paper Detail 보조 콘텐츠, 근거 참조 검증, 선택적 결과 wrapper |
| `tools/contracts.py` | 3개 Tool의 입력/출력 Pydantic 모델, EvidencePackage |
| `registry.py` | ToolName/ToolContract 단일 출처, JSON Schema 생성, ToolSpec/Registry, 빈 운영 Registry |
| `executor.py` | 입력/출력 검증, Handler 실행, JSON 직렬화, 안전한 오류/메타데이터 로그 |
| `_execution.py` | 동기 호출 대기 timeout, 요청 deadline 전파, 제한된 daemon worker |
| `prompts.py` | PromptBuilder Protocol, 최소 정책과 DefaultPromptBuilder |
| `agent_loop_runner.py` | 호출 루프, 한 응답의 여러 Tool 순차 실행, 상한, 최종 결과 |
| `workflows/feed.py` | 정식 Context 준비 → Feed 실행 → 검증된 Backend Handoff의 세 단계 |
| `workflows/feed_demo_compat.py` | 외부 recommendations API만 위한 임시 UserProfile/Demo 호환 경계; 전환 후 제거 |
| `workflows/feed_context_prepare.py` | 정식 전체 Loader 연결·allowlist·직렬화·고정 오류; 기존 API import용 지연 shim |
| `workflows/feed_execution.py` | Feed Prompt/검색 전용 Registry/3·3 설정 검증 및 공용 structured Runner 호출 |
| `workflows/feed_context*.py` | Production V1.1/Loader 및 실제 Backend Snapshot Source (실 DB/HTTP 활성화 검증은 별도) |
| `testing/feed_workflow_fakes.py` | 실제 Production Source/Loader/Prepare에 합성 FeedReadSnapshot/LLM/Tool/Handoff를 연결 |
| `tests/test_feed_workflow.py` | Mapper → Execution → Runner → V1 Handoff 통합 및 실패 차단 검증 |
| `clients/base.py` | Provider-independent LLMClient Protocol |
| `clients/openai_client.py` | SDK 주입, 모델 설정 주입, Chat Completions 변환/정규화 |
| `tools/evidence_tools.py` | 미래 RAG 서비스의 EvidenceRetriever Protocol; 실행 구현 없음 |
| `testing/fake_tools.py` | 고정 합성 fixture 기반 3개 Fake Handler/테스트 Registry |
| `testing/fake_llm.py` | 정해진 ModelTurn 순서와 요청 snapshot을 사용하는 FakeLLMClient |
| `testing/demo.py` | 최근 기록 → 근거 조회 → 최종 응답의 Fake 데모 |
| `testing/structured_demo.py` | 같은 Tool Loop로 실행하는 Chat/Feed V1 오프라인 예시 |
| `tests/test_outputs.py` | V1 경계/버전/참조/구조화 실행/legacy 호환성 검증 |
| `tests/test_workflows.py` | A–D 워크플로, 오류, 경계/Builder 교체/Fake 계약 검증 |
| `tests/test_openai_client.py` | 네트워크 없이 실제 SDK 메시지 형식/오류 검증 |
| `tests/test_provider_integration.py` | 명시적 opt-in으로만 실행하는 외부 Provider 테스트 |
| `tests/test_readiness.py` | timeout/deadline, 실패 경계, 정본 등록, 출력 일관성 회귀 검증 |
| `README.md` | 사용법, 계약, 보안 경계, 후속 연결 사항 |

`ai/`, `agent/`, `tools/`, `clients/`, `testing/`, `tests/`의 `__init__.py`는 패키지 표식이다.
자동 Tool 등록이나 DB/API 호출 부작용은 없다.

## Tool Contract

아래 이름/설명/모델은 `registry.TOOL_CONTRACTS`가 단일 출처다.
LLM JSON Schema는 `input_model.model_json_schema()`에서 생성하여 이중 정의하지 않는다.
모든 모델은 알 수 없는 필드를 거부한다. 모든 입력에서 `patient_id`, `user_id`는 제외한다.
UUID는 레포 실제 타입을 따른다. 날짜는 ISO 날짜, 시각은 datetime JSON으로 전달한다.

| Tool / 목적 | Input → Output 모델 | 주요 입력 | 주요 출력 |
|---|---|---|---|
| `get_patient_profile`: 저장된 기본 환자 프로필 snapshot | `PatientProfileInput` → `PatientProfileOutput` | `{}` (추가 argument 금지) | `name?: str`, `dementia_stage?: str`, `diagnosis_date?: date`, `symptoms: list[str]`, `interests: list[str]` |
| `get_recent_care_logs`: 최근 patient_care 기록 | `RecentCareLogsInput` → `RecentCareLogsOutput` | `days: strict int=14 (1..90)`, `limit: strict int=10 (1..30)` | `period: {start_at,end_at}`, `logs: list[CareLogItem]`, `total_count: strict int>=0` |
| `search_evidence`: RAG 근거 | `EvidenceSearchInput` → `EvidencePackage` | `query: str (1..2000자)`, `top_k: int=5 (1..20)` | `query`, `evidence: list[EvidenceItem]` |

`CareLogItem`: `logged_at: aware datetime`, `content: str | None=null`, `mood_tag: str | None=null`.
기간의 `start_at`/`end_at`도 timezone-aware이며 `start_at <= logged_at < end_at`을 검사한다.
`logs`는 실제 시각 기준 최신순(동일 시각 허용)이며 `total_count >= len(logs)`다.
DB ID와 log_type은 결과에 노출하지 않고 동일한 visible data의 기록도 허용한다.

`EvidenceItem`: 필수 `evidence_type: new_research/guideline`, `title: str`,
`relevance_score: float (-1..1, 기존 코사인 점수 범위)`.
선택 필드: `pmid: str | None`, `publication_year: int | None`, `study_type`, `ai_summary`,
`abstract`, `journal`, `doi`, `organization`, `source_url` (기본 null).
`full_text_available: bool`은 기본 false다. `query` + `evidence` 묶음과 각 필드의 타입/기본값은
`app.ai.retrieval.search.EvidencePackage`의 직렬화 형식과 일치한다.
DB 내부 `paper_id`/`external_id`/`published_date`는 이 RAG 경계에서 받지 않는다.
Agent의 extra 금지 및 유한 코사인 점수 범위 검증은 유지한다.
선택 필드는 반환하지 않은 정보를 추측해서 채우지 않는다.

예시의 `age/sex/cognitive_status`는 현재 환자 스키마에 없으므로 추가하지 않았다.
실제 `dementia_stage/diagnosis_date/symptoms/interests`를 우선했다.
`conditions/care_environment`는 `get_patient_profile V1` 범위에서 제외한다.
돌봄 기록은 DB의 `logged_at`을 사용하며 `patient_care` 조건은 서버 내부 의미다.

### get_patient_profile V1 semantics

`get_patient_profile`은 trusted server `AgentContext`가 지정한 환자의 **DB에 저장된 기본
프로필 snapshot**을 읽기 전용으로 조회하는 Tool이다. Chat/Feed 등의 개인화 context가
필요할 때 사용한다. 아래 의미는 V1 계약이며 현재 Handler는 deterministic 합성 Fake다.
Real Handler, Backend Adapter, DB 조회 함수와 production 등록은 후속 작업이다.

대상 환자는 서버의 `AgentContext.patient_id`/`user_id` 및 서버 측 인증/접근권한 경계가
결정한다. LLM-visible input은 `{}`이며 `patient_id`, `user_id`, `caregiver_id`,
`include_conditions`, `include_care_environment` 등 추가 argument를 모두 거부한다.
`PatientProfileOutput`에도 `patient_id`를 포함하지 않는다. 단, 현재 `PromptBuilder`는
trusted `AgentContext` 자체를 system message로 직렬화하므로 `patient_id`/`user_id`가
LLM prompt에 전달된다. 이 계약은 LLM이 UUID를 전혀 볼 수 없음을 보장하지 않는다.
Context ID의 prompt redaction은 별도 보안/Prompt-boundary 후속 과제다.

출력은 다음 5개 필드만 포함하며 저장되지 않은 정보를 추정하거나 생성하지 않는다.

- `name`: 저장된 표시 이름/별칭. 법적 실명을 보장하지 않으며 미등록이면 null이다.
- `dementia_stage`: 저장된 stage 문자열(`str | None`). 최신 임상 평가 결과나 임상적으로
  확정된 현재 단계를 보장하지 않는다. CDR, 임상 평가, 돌봄 기록 등으로 새로 판정하거나
  보정·정규화하지 않는다. 미등록이면 null이며 enum/MCI 분류는 이번 V1에서 결정하지 않는다.
- `diagnosis_date`: 저장된 진단일. 미등록이면 null이며 다른 정보에서 추정하지 않는다.
  Pydantic `date`의 기존 ISO JSON 직렬화를 사용한다.
- `symptoms`: 프로필에 등록된 증상 항목/태그. 임상적으로 확인된 전체 증상 목록이 아니며
  다른 기록에서 증상을 추가하지 않는다. `[]`는 저장된 항목이 없다는 뜻이며 무증상을 뜻하지 않는다.
- `interests`: 프로필에 등록된 관심 치료/관리 분야. 의료적으로 권장된 치료 목록이 아니다.
  `[]`는 저장된 항목이 없다는 뜻이며 실제 관심이나 관리 필요성이 없다는 뜻이 아니다.

`name`, `dementia_stage`, `diagnosis_date`가 모두 null이고 `symptoms`, `interests`가 모두
`[]`인 출력도 **성공적으로 조회된 sparse profile**일 수 있다. 이는 profile row가 존재하고
선택 정보가 미등록이라는 뜻이며, dementia/MCI가 아님·진단받은 적 없음·증상/관심 없음으로
해석하면 안 된다. 저장된 profile data는 독립적으로 검증된 임상 사실 또는 Agent inference와
구분한다. 예를 들어 “프로필에 경도 단계가 저장되어 있다”는 설명은 가능하지만,
이를 “현재 경도 치매가 임상적으로 확진되었다”로 바꾸지 않는다.

대상 profile 자체를 찾지 못함, 접근 권한 문제, DB 조회 실패, timeout, Handler exception,
Tool output contract 위반은 실패다. 이러한 실패를 null/`[]`의 sparse success로 위장하지 않는다.
현재 Executor의 `empty_result`, `invalid_output`, `tool_exception`, `tool_timeout`을 재사용하며,
실제 DB not-found/access-denied 처리와 row identity/권한 검증은 향후 Real Handler의 책임이다.

최근 돌봄 기록, `profile_summaries`, 임상 평가, 복약, 안전/행동 이벤트, 병원 방문 기록을
조회하거나 결합하지 않는다. `conditions`, `care_environment`, caregiver relationship,
`reading_level`, 사용자/간병인 ID는 출력 범위에 없다. 장기 patient-memory semantic retrieval,
embedding 검색/생성/저장, dementia stage 재판정 또는 의료적 진단/추론 Tool이 아니다.
향후 Real Handler에서도 read-only semantics를 유지하며 profile 수정, DB write,
annotation 저장, semantic memory 갱신 등의 side effect를 수행하지 않는다.

### get_recent_care_logs V1 semantics

`get_recent_care_logs`는 trusted server `AgentContext`가 지정한 현재 사용자/환자에 대해,
간병인이 저장한 최근 **patient_care 돌봄 기록**을 제한된 범위에서 읽기 전용으로 조회해
개인화 context를 제공하는 Tool이다. 현재는 합성 Fake만 존재한다. 아래 조회/권한 의미는
V1 계약이며 실제 DB 조회 함수, Backend DTO/Adapter, Real Handler, production 등록은 후속이다.

LLM-visible input은 `days`와 `limit`뿐이다. 두 값은 strict integer이며 문자열 숫자/bool/float를
거부한다. `days`는 기본 14, 1..90이고 `limit`은 기본 10, 1..30의 최대 반환 개수다.
`patient_id`, `user_id`, `caregiver_id`, `log_type`, `log_types`와 임의의 추가 필터는 받지 않는다.
대상 사용자/환자와 종료 시각을 LLM argument로 선택하거나 바꿀 수 없다.
기존 PromptBuilder의 trusted Context system message와 ID 전달은 이번에 변경하지 않는다.

현재 단일 caregiver DB 구조에서 **향후 Real Handler/Backend Adapter가 강제할 backing semantics**:

1. 먼저 `patient_profiles.id == context.patient_id`인 환자의
   `patient_profiles.caregiver_id == context.user_id`인지 서버에서 검증한다.
2. 조회 row는 `care_logs.patient_id == context.patient_id`,
   `care_logs.user_id == context.user_id`, `care_logs.log_type == "patient_care"`를 모두 만족해야 한다.

이는 현재 Fake가 실제 접근 권한을 검증한다는 뜻이 아니다. 권한/identity 실패를 빈 logs로
위장하지 않는다. DB query, 권한 적용, timeout/cancellation 및 production 연결은 후속 책임이다.

`end_at`은 Handler가 요청당 한 번 결정한 조회 종료 시각이고,
UTC 기준으로 `start_at = end_at - timedelta(days=args.days)`다. 달력 날짜 N개가 아닌 최근 **N×24시간
rolling window**이며 DST 등에서도 UTC instant 기준으로 N×24시간을 뺀다.
조회 범위는 `start_at <= logged_at < end_at`(start inclusive / end exclusive)이고
`start_at < end_at`이어야 한다. 모든 시각은 timezone-aware이며 offset이 달라도 실제 instant로
비교한다. `logged_at`은 DB의 기록 시각이며 content가 서술하는 사건/증상의 발생 시각으로 추론하지 않는다.

향후 Real Handler는 종료 시각을 timezone-aware instant로 결정하고, duration 계산 전에 UTC로 정규화한다.

```python
end_utc = end_at.astimezone(timezone.utc)
start_utc = end_utc - timedelta(hours=24 * args.days)
```

DB의 `TIMESTAMPTZ` 조회 경계도 instant 기준의 `start_utc <= logged_at < end_utc`로 적용한다.
local wall-clock timezone datetime에 직접 `timedelta(days=...)`를 적용하는 계산에 의존하지 않는다.
DST spring-forward/fall-back에서도 실제 elapsed duration은 항상 `days × 24시간`이어야 한다.
이는 후속 구현 규칙이며 현재 Real Handler는 없다. 향후 Handler 테스트에서는 두 DST 전환 모두
`days=1`의 UTC elapsed가 정확히 24시간인지 확인한다.

출력은 `period`, `logs`, `total_count`뿐이다. `period`는 `start_at`, `end_at`만,
각 `CareLogItem`은 `logged_at`, `content`, `mood_tag`만 포함한다. 환자/사용자/간병인 ID와
`log_id`/`log_type`은 결과에 노출하지 않는다. 로그는 `logged_at` 최신순(non-increasing)이고
같은 시각이나 완전히 같은 visible data도 허용한다. 임의의 duplicate rejection을 하지 않는다.
향후 DB ordering은 `ORDER BY logged_at DESC, id DESC`다. `id DESC`는 UUID가 더 최신이라는
뜻이 아니라 동일 시각의 내부 deterministic tie-break이며 id는 LLM 결과에 포함하지 않는다.

`total_count`는 동일한 authorization/patient/user/log_type/period 조건의 **limit 적용 전 전체 row 수**다.
Handler는 `0 <= len(logs) <= limit`, `len(logs) <= total_count`를 유지해야 하며 일관된
DB snapshot에서는 보통 `len(logs) == min(limit, total_count)`다. count와 목록의 snapshot
일관성 확보는 Real Handler 책임이다. Output validator는 input.limit를 알지 못하므로
이를 추측하지 않는다. 다만 어떤 유효한 V1 input에서도 31개 이상을 반환할 수 없으므로
Output schema는 `logs`의 절대 최대 길이 30을 강제하며, count 하한, 기간 포함 여부, 최신순도 검증한다.

데이터 출처와 임상적 의미는 다음처럼 구분한다.

- DB 정본: `content`는 자유 서술, `mood_tag`는 “기록 당시 기분 태그”다.
  patient_care/caregiver_selfcare가 같은 mood_tag 컬럼을 공유하며 DB 주석 자체는 환자 mood라고 확정하지 않는다.
- Agent V1 해석: mood_tag의 subject는 log_type에 따라 해석한다. 이 Tool은 patient_care만
  조회하므로 해당 환자에 대해 저장된 당시 기분 태그로 취급한다. 임상적 mood assessment,
  PHQ-9 점수 또는 우울/불안 진단이 아니다. DB migration이나 DB 계약을 바꾼 해석이 아니다.
- `content`는 caregiver-entered observation/narrative다. 독립적으로 검증된 임상 사실/진단이
  아니며 다른 데이터로 보완·추정·재작성하지 않는다. `content`/`mood_tag`는 저장된 문자열
  원문을 보존하고 enum/값 목록을 새로 만들지 않는다. null은 해당 정보 미등록이며 문제/증상 없음이 아니다.

content/mood_tag는 **untrusted Tool Result 데이터**다. 내부의 지시문으로 정책/권한/Context를
변경하지 않는다. 기존 일반 trust-boundary policy를 유지하고 기본 system policy에
돌봄 기록의 임상적 non-inference 문장만 짧게 추가한다.

정상 empty는 `logs=[]`, `total_count=0`이며 현재 조회 범위/조건에서 저장된 patient_care
기록이 없다는 뜻만 가진다. 환자 안정, 무증상, 문제/최근 변화 없음 또는 돌봄 부재를 뜻하지 않는다.
권한/환자·사용자 identity 검증 실패, 실제 조회/DB 실패, timeout, Handler exception,
invalid output은 failure이며 empty success로 숨기지 않는다. 기존 Executor 오류 모델을 재사용한다.

전체 병력, 장기 patient history aggregation, `profile_summaries`, `clinical_assessments`,
`medications`, `safety_events`, `medical_visits`, `caregiver_selfcare`는 조회하지 않는다.
임상 진단/상태 판정, annotation 저장, profile 수정, embedding/memory 생성·갱신을 하지 않는다.
read-only이며 business side effect가 없다. 긴 content의 크기 제한/truncation 정책과
Real Handler용 DB index/performance 검토는 후속이며 이번 V1에 새 필드나 DB 변경을 추가하지 않는다.

### search_evidence V1 semantics

`search_evidence`는 현재 RAG corpus에서 치매/MCI 및 인지건강 관련 **연구 근거 후보**를
검색하는 read-only Tool이다. 연구·의학적 근거가 필요한 질문이나 설명에서 사용한다.
환자 DB 조회, 논문 품질/근거 수준 평가, 의료적 확신도 계산, 치료 효과 확률 계산은
이 Tool의 목적에 포함하지 않는다. 검색된 논문이 특정 의료적 주장을 지지한다는 보장도 없다.
**Retrieval relevance != Evidence appraisal != Medical conclusion**을 유지한다.

**입력:** `query`는 1..2000자이며 사용자 원문 또는 Workflow/Agent가 검색 목적에 맞게
재구성한 질문을 허용한다. 임상/돌봄 맥락은 포함할 수 있지만 검색에 불필요한 직접 식별정보
(patient/user UUID, 이름, 전화번호, 주소 등)는 최소화하여 query에 포함하지 않는다.
이 원칙은 의미 정책이며 개인정보 검출 validator를 구현한 것은 아니다.
Agent input validator는 whitespace-only query를 거부하고, nonblank query를 trim/정규화/재작성하지 않는다.
현재 RAG `search()`는 자체적으로 query를 strip한다. Agent 입력 원문 보존과 RAG 내부 처리는 구분한다.
따라서 현재 RAG가 반환하는 `EvidencePackage.query`는 검색 실행 전에 `(query or "").strip()`으로 정규화된 검색 query를 나타내며, Agent가 제출한 원문과 항상 byte-for-byte 동일하지는 않다.
LLM input은 `query`, `top_k` 두 필드뿐이며 `patient_id`/`user_id`/`exclude_animal`은 노출하지 않는다.
`top_k`는 StrictInt, 기본 5, 범위 1..20인 **최대** 반환 개수다. 정확히 N개를 채우는 보장은 없다.

**검색 책임과 점수:** query embedding은 RAG 영역의 BGE-M3가 담당하고 DB 검색은
pgvector cosine similarity 기반이다. 현재 별도 reranker는 없다. Agent/향후 Handler는
임베딩·검색 알고리즘을 구현하는 대신 RAG를 호출하고 반환 계약을 검증한다.
`relevance_score`는 cosine-similarity 기반 retrieval score이며 Agent는 유한한 [-1, 1] 값을 허용한다.
논문 품질, evidence level, 의료적 확신도, 치료 효과 또는 환자 적합도 점수로 해석하지 않는다.
RAG 모델의 Field description에는 현재 0~1 표현이 있으나 Agent의 [-1, 1] 계약과 계산 방식은 유지한다.

**동물 연구 정책:** 보호자에게 사람 대상 근거를 제공하기 위해 향후 Real Adapter는
server-side RAG policy로 `exclude_animal=True`를 전달해야 한다. LLM이 이 정책을 선택하지 않는다.
제외 대상은 RAG classifier가 동물/세포 전용 연구로 판단한 결과다. 인간 연구 신호가 있는
mixed study를 단순히 동물 관련 표현 때문에 제외한다는 뜻은 아니다.
세부 판별 알고리즘의 정본은 `app.ai.retrieval.evidence`이며 이번 계약에서는 재구현하지 않는다.

**순서와 참조:** `EvidencePackage.evidence`의 순서는 RAG가 반환한 순서가 정본이다.
Agent와 향후 Handler/Adapter는 임의 재정렬하지 않는다. Structured Output의 authoritative reference는
성공한 `search_evidence`의 `tool_call_id`와 해당 호출의 `evidence[]` 배열에 대한 **0-based**
`evidence_index` 쌍이다. 기존 `outputs.py`의 참조 검증/metadata 복사를 그대로 사용한다.

**성공과 실패:** `evidence=[]`, `top_k`보다 적은 결과, 동물 필터링 후 결과 감소,
개별 candidate의 상세 데이터 누락에 따른 결과 감소는 정상 성공이 될 수 있다.
`get_papers_by_ids()`는 존재하지 않는 candidate를 제외할 수 있으므로 향후 검색 경계에서는
그 누락만으로 전체 검색을 반드시 실패 처리하지 않고 나머지 정상 evidence를 유지할 수 있다.
전체 RAG/DB 실행 exception, timeout, Tool output contract 위반은 별개의 실패이며
이를 빈 성공 package로 바꿔 숨기지 않는다.
빈 결과의 의미는 "현재 corpus와 query/검색 정책에서 반환 가능한 근거가 없었다"로 제한한다.
치료 효과 없음, 행동의 안전함, 세상에 관련 연구가 없음 또는 RAG 장애를 의미하지 않는다.

**Metadata와 운영 경계:** 기존 PMID/DOI/저널/출판연도/URL/제목/연구 유형/기관 등의
metadata를 유지한다. nullable metadata가 없으면 null이며 Agent/LLM이 추정하거나 생성하지 않는다.
스키마는 `new_research | guideline`을 허용하지만 현재 실제 검색은 `new_research`를 반환한다.
실제 RAG가 제공하지 않은 guideline을 Agent/Adapter가 만들거나 임의 승격하지 않는다.
V1에는 `paper_id`, `source_id`, `corpus_updated_at`, `authors`, evidence level A/B/C/D 필드나 mapping을 추가하지 않는다.
이 Tool은 환자/DB 데이터를 변경하거나 결과를 저장하는 side effect가 없다.
Real Handler/Adapter, `top_k → k` 변환, 정책 전달 및 실연결 검증은 후속 작업이며
현재 `production_registry()`는 계속 비어 있다. `EvidencePackage`의 기존 필드 구조를 유지한다.

## Context / Prompt 경계

`AgentContext`는 `request_id: str`, `user_id: UUID`, `patient_id: UUID`, `locale=ko-KR`를 담는
불변 모델이다. 인증/소유권을 검증한 서버가 생성해야 한다. 이것만으로 인증을 구현한 것은 아니다.
Tool Handler는 `(context, validated_input)`을 받는다. LLM이 patient/user ID를 입력하면 검증에서 거부한다.
Executor에는 출력 모델에 `patient_id`가 있을 때 Context와 비교하는 일반 검사가 남아 있다.
`PatientProfileOutput V1`에는 이 필드가 없으므로 해당 검사는 profile에 적용되지 않는다.
향후 Real Handler는 출력의 5개 필드로 projection하기 전에 조회 row의 identity와 접근 권한을
검증해야 한다. 출력 ID 제거 또는 schema 검증만으로 실제 환자 소유권 검증을 대체하지 않는다.

PromptBuilder를 생성자에 주입한다:

```python
engine = AgentLoopRunner(client, registry, prompt_builder=DefaultPromptBuilder())
result = engine.run(user_input, trusted_context)
```

Builder의 교체 지점:

- `build_system_prompt(context)`
- `build_initial_messages(user_input, context)`
- `build_tool_followup_messages(turn, outcomes)`
- `build_final_response_context(evidence_packages)`

기본 Builder는 정책(system), 서버 Context(system의 별도 JSON), 원문 사용자 입력(user),
모델 Tool Calls(assistant), 호출 ID별 결과(tool), 구조화 근거 묶음(user의 별도 데이터 message)을 유지한다.
사용자 입력은 strip/요약/재작성하지 않는다. Tool Result/Evidence를 system 정책으로 승격하지 않는다.
근거 묶음은 재판단 시 Builder가 제공하며, 원래 Tool 결과도 conversation에 보존한다.
Provider로 전송할 때 `kind`는 제거되지만 role, 개별 message, Tool Call ID/JSON 경계는 유지된다.
Query Rewriting/Intent Extraction/Compression은 이 Builder를 교체하여 추가할 수 있다.
현재는 구현하지 않았으며 원문 보존/신뢰 경계를 유지하는 책임은 새 Builder에도 적용된다.

입력 문자열로 시스템 메시지나 Tool JSON/서버 Context 객체를 덮어쓸 수 없도록 테스트했다.
이 구조와 정책은 모델의 모든 Prompt Injection 대응이나 의료 답변의 정확성을 보장하지 않는다.
실제 접근 권한은 서버/Handler에서 강제해야 한다. 운영 쓰기 Tool은 소유권, 재실행,
승인/감사 정책을 합의한 후 연결한다.

## 실행 제한 / 오류 / 로그

기본 상한: Tool round 5, 전체 Tool 호출 10. 두 값은 strict positive integer만 허용하며
0/음수/bool/float/NaN/infinity를 거부한다. 한 턴의 여러 호출은 순차 실행한다.
상한을 초과할 batch는 일부만 실행하지 않고 모두 거부한다. 정확히 상한을 실행한 뒤
추가 Tool 없는 최종 LLM 응답은 허용한다. 따라서 LLM 호출 수는 최대 6회다.
같은 Tool의 반복은 상한으로 종료하고, 중복 Call ID는 재실행하지 않고 `invalid_response`로 종료한다.
정상적인 같은 Tool 재조회와 반복을 구분하는 의미적 중복 제거는 이번 단계에서 구현하지 않는다.

Tool 오류는 `unknown_tool`, `invalid_arguments`, `tool_exception`, `empty_result`,
`invalid_output`, `serialization_error`로 LLM에 전달한다. Tool 오류 후 후속 Tool이나
최종 답변이 가능하다. Output의 빈 list는 정상적인 데이터 없음이며, `None`은 오류다.
직렬화 전에 출력 모델을 다시 검증한다. validation/exception 상세값은 외부에 포함하지 않는다.
Python `TimeoutError`(내부 실행 timeout 포함), HTTPX `TimeoutException`, OpenAI
`APITimeoutError`는 `tool_timeout`으로 처리하여 Agent를 failed로 종료한다.
예외 이름/메시지로 추측하지 않으며 일반 연결 오류 등은 기존 `tool_exception`을 유지한다.

기본 Handler 대기 제한은 30초, Agent 전체 request deadline은 120초다.
기존 생성자에 선택적 keyword-only 옵션을 추가했다:

```python
engine = AgentLoopRunner(client, registry,
    tool_timeout_seconds=30.0, request_timeout_seconds=120.0)
```

`ToolExecutor(registry, timeout_seconds=30.0)`도 직접 설정할 수 있다.
초 단위 값은 양의 유한 수만 허용하며 플랫폼 thread 대기 한도를 넘지 않아야 한다.
요청 deadline은 monotonic clock을 사용하고 round마다 초기화하지 않는다.
Builder, Registry definitions, LLM, ModelTurn 검증, Executor, Evidence 검증의 대기를 제한한다.
ContextVar로 deadline을 전파하고 종료 시 복원하므로 동시 요청 간 deadline을 공유하지 않는다.

동기 확장 지점은 제한된 daemon thread에서 실행한다. 일반 작업 최대 32개,
Handler 최대 16개이며 두 용량을 분리해 Executor/Handler 간 slot 교착을 방지한다.
용량 대기 시간도 timeout/deadline에 포함하며, 만료된 대기 작업은 시작하지 않는다.
timeout는 늦은 결과를 폐기하고 Agent를 failed로 종료하며 남은 batch/LLM 후속 호출/자동 재시도를 중단한다.
**이 대기 제한은 실행 중인 Python thread의 강제 종료나 저장 rollback을 보장하지 않는다.**
늦은 Handler가 계속 실행할 수 있으며 slot은 실제 종료 시 해제된다.
Python 실행 기회를 장시간 막는 native 코드에 대한 hard real-time deadline도 보장하지 않는다.
실제 DB/RAG 연결 시 SDK/DB statement timeout, 취소, 저장 멱등성을 별도로 적용해야 한다.

Builder/Executor/Evidence/Registry의 실행 예외는 고정 오류 코드와 failed로 반환하고
원래 예외/환자 데이터는 포함하지 않는다. 일반 Provider 오류·잘못된 응답의 기존 상태는 유지한다.
Executor의 outcome/trace는 상태에 추가하기 전에 재검증하고 호출 ID/이름/성공 상태를 검사한다.
최종 AgentResult 검증에 실패하면 오염된 metadata와 답변을 버리고
`result_validation_error`를 담은 최소 failed 결과를 반환한다.
Registry는 callable Handler, 구체적인 Pydantic 모델과 JSON Schema, 이름/설명/입출력 모델의
정본 ToolContract 일치를 검증한다. test mode도 정본 검증을 우회하지 않는다.

AgentResult 상태: `completed`, `limit_reached`, `provider_error`, `invalid_response`, `failed`.
`completed`는 루프가 최종 답변까지 완료되었다는 뜻이다. 중간 Tool 오류는 `errors`에 남고,
실제 의료 정확성/모든 Tool 성공을 뜻하지 않는다. Provider 실패와 빈/잘못된 응답은
최종 답변 없는 명확한 실패 상태로 반환한다.

기존 41개 기본 테스트의 이름과 검증 범위를 유지했다. 0 상한 검증은 새 정책에 맞춰
생성자 거부를 확인하며, 직렬화 실패는 정본 계약 대신 serializer에 실패를 주입한다.

로그: 서버 request_id, 등록된 tool_name, 성공 여부, 실행 시간, 결과 건수, 고정 오류 코드만 기록한다.
Tool arguments/result, 사용자 질문, 환자 기록, 전체 prompt, 예외/DB/키 내용은 기록하지 않는다.
외부 AgentResult도 전체 messages/reasoning을 포함하지 않는다.
FakeLLM의 메모리 snapshot은 합성 테스트 전용이며 운영 기록으로 사용하지 않는다.

## Fake / 운영 구분

`testing.fake_tools.fake_registry()`만 3개 Fake Tool을 등록한다.
Fake ToolSpec은 `test_only=True`이며 production Registry 등록 시 오류가 난다.
`production_registry()`는 의도적으로 비어 있다. 동작하는 척하는 운영 stub은 없다.
최근 기록 Fake는 `FIXTURE_NOW=2026-10-06T12:00:00Z` 기준 rolling window/최신순/limit을 사용하고,
`total_count`는 limit 적용 전 개수다. 동일 시각 hidden UUID tie-break와 nullable fixture도 결정적이다.
실제 권한 검증/DB 조회는 하지 않는다.
모든 근거 ID/DOI/PMID는 명백한 FAKE fixture다.

## 다음 실제 연결 단계: 팀 합의 필요

현재 DB에는 논문 수집/조회/분석/임베딩 함수와 사용자·환자 조회 helper가 있다.
사용자·환자 aggregate와 완전한 Feed Source 계약의 차이 및 Backend blocker는 위 V1.1을 따른다.
3개 Tool 중 **현재 계약만으로 바로 운영 연결 가능한 Tool은 없다**. 실제 연결은 다음 작업이다.

| Tool | 필요한 backing 계약/합의 |
|---|---|
| 프로필 | Context의 사용자/환자 소유권과 row identity를 검증하는 조회 함수; sparse success와 profile not-found/failure 구분 후 V1 5개 필드로 변환 |
| 최근 기록 | V1은 trusted user/patient, patient_care only, rolling datetime window로 확정; 실제 Backend DTO/DB 조회 함수, authorization 적용, count/list snapshot 일관성, production Handler는 후속 |
| 근거 검색 | `EvidenceRetriever.search_evidence(query, top_k, context) -> EvidencePackage`; query embedding과 검색은 RAG 담당 |

기존 `search_similar(embedding, top_k, model_name)`는 UUID+score만 반환하고,
`get_papers_by_ids(paper_ids)`는 논문 상세를 반환한다. 현재 `app.ai.retrieval.search.search`
구현이 query embedding과 검색/상세 조회를 제공하지만 Agent의 운영 Handler에는 연결하지 않았다.
다음 Adapter에서 `top_k` → `k`, Context 전달 정책과 Agent EvidencePackage 변환을 처리한다.
내부 누락 ID는 `paper_id`로 매칭해야 하며 리스트 위치만으로 점수를 결합하면 안 된다.
RAG의 `evidence_type="new_research"` 고정과 full_text_available 기본 false는 이번에 변경하지 않았다.
care annotation persistence는 향후 `CareAnnotationWorkflow`가 structured output 검증/guardrail 후
deterministic하게 수행할 책임이다. 해당 Workflow와 DB persistence는 아직 구현하지 않았다.
실제 RAG와 DB 계약/공용 스키마를 이번 작업에서 수정하거나 새로 가정하지 않았다.

## 최종 답변 Structured Output V1 (선택적)

`engine.run(user_input, context)`는 기존 `AgentResult` 및 문자열 `final_answer`를 그대로
반환한다. 기존 결과 JSON에 새 필드가 추가되지 않는다. FakeLLM/LLMClient의
`generate(messages, tools) -> ModelTurn`도 그대로다.

```python
from app.ai.agent.outputs import ChatAnswerV1, FeedAnswerV1

chat = engine.run_structured(user_input, context, output_model=ChatAnswerV1)
feed = engine.run_structured(user_input, context, output_model=FeedAnswerV1)
```

`run_structured()`만 서버가 선택한 최종 JSON Schema/콘텐츠 정책을 system message로
추가한다. 기존 PromptBuilder 교체 지점과 Tool 호출/실행/재판단 루프는 유지한다.
구조화 정책은 첫 user message 직전에 삽입하며 Builder의 기존 메시지 순서는 보존한다.
기본 순서는 system 정책 → system Context → system 구조화 정책 → user 원문이다.
Structured 모드에서는 `build_final_response_context()`의 aggregate evidence copy를 생략한다.
call ID가 있는 원래 Tool Result의 전체 EvidencePackage가 재판단/최종 생성에 계속 전달된다.
legacy `run()`은 기존 aggregate evidence message와 Builder 호출 동작을 유지한다.
Tool Call이 없는 마지막 `ModelTurn.text`를 단일 JSON object로 검증한다.
Provider native response_format/strict schema 기능은 연결하지 않았다. 모델이 프롬프트를
따르지 않으면 서버 검증에서 거부한다. 자동 JSON 수정/markdown 추출/재요청은 하지 않는다.

반환 `StructuredAgentResult`는 `execution: AgentResult`, `output: ChatAnswerV1 | FeedAnswerV1 | None`,
`sources: list[ResolvedEvidence]`의 별도 wrapper다. Chat 성공 시 `execution.final_answer`는
검증된 `output.answer`에서만 파생한다. Feed 성공 시 `execution.final_answer=None`이며,
JSON을 문자열 답변에 넣지 않는다. 실패 시 output=None, sources=[], final_answer=None이다.
참조/JSON/콘텐츠 계약 위반은 `invalid_response` + `invalid_structured_output`으로 반환하고,
원문/검증 상세를 오류에 노출하지 않는다. 최종 검증도 기존 전체 request deadline에 포함된다.
Tool 오류 후 참조 없는 Chat 설명은 가능하며, `completed`는 의료적 정확성 보장이 아니다.

### 콘텐츠 모델과 제한

모든 아래 모델은 `AgentModel`의 extra 금지/비유한 수 금지 정책을 상속한다.
콘텐츠 문자열은 strict string이며 빈 문자열/공백만 있는 문자열을 거부하고 원문을 보존한다.
`schema_version`과 `response_type`은 기본값 없이 명시적으로 제출해야 한다.

| 모델 | 필드 및 제한 |
|---|---|
| `EvidenceReference` | `tool_call_id`: 1..256자, `evidence_index`: strict int >=0, 0-based |
| `ChatAnswerV1` | `schema_version="1"`, `response_type="chat"`, `answer`: 1..12,000자, `citation_refs`: 0..20개 고유 참조 |
| `FeedContentItemV1` | `source_ref`, `headline`: 1..200자, `summary_bullets`: 1..5개/각 1..500자, `personal_reason`: 1..1,000자, `category`: 단일 treatment/care/prevention/diagnosis |
| `FeedAnswerV1` | `schema_version="1"`, `response_type="feed"`, `items`: 0..5개, 동일 source_ref 중복 금지 |
| `PaperParagraphV1` | `text`: 1..2,000자 |
| `PaperBodyV1` | `easy`, `detail`: 각각 1..10개 PaperParagraphV1 |
| `GlossaryEntryV1` | `term`: 1..100자, `meaning`: 1..500자 |
| `PaperDetailContentV1` | `schema_version="1"`, `response_type="paper_detail"`, `source_ref`, summary_bullets(Feed와 동일), body, `personal_meaning`: null 또는 1..2,000자, `limitations`: 0..5개/각 1..500자, `glossary`: 0..10개 |

빈 Feed는 검색/필터링 후 결과가 없거나 개인화 근거가 부족한 정상 콘텐츠다.
정확히 5개를 강제하거나 없는 근거를 채우지 않는다. 순위는 생성된 items 순서이며,
최신성/Top-K 선택 정책 자체를 구현하거나 검증한 것은 아니다.
최종 Chat/Feed JSON 문자열은 파싱 전에 65,536자로 제한한다. 중복 JSON key,
NaN/Infinity, 잘못된 version/type, metadata/미래 V2 필드도 거부한다.

Paper Detail은 기존 summary_bullets/근거 참조를 재사용하는 보조 계약만 추가했다.
독립 실행 모델로 `run_structured()`에 전달할 수 없고 Workflow/API/저장은 없다.
개인화 Context가 없을 때 personal_meaning은 명시적으로 null이다. 문단별 citation 번호는
생성하지 않는다. 전체 콘텐츠의 source_ref만 검증하며 문단별 인용은 후속 별도 계약이다.
`body.easy/detail`은 현재 화면의 읽기 난이도와 맞춘 필드이며 미래의 `easy_summary`가 아니다.
현재 상세 화면의 복수형 `limitations`도 미래 3분할의 단수형 `limitation`과 별개다.

### 출처 참조 검증과 서버 조립 경계

AgentLoopRunner는 Executor 성공 및 EvidencePackage 재검증을 통과한 `search_evidence` 결과만
요청 내부 `evidence_by_call`에 call ID로 보관한다. 원래 Tool Result payload는 바꾸지 않는다.
모델은 role=tool message의 tool_call_id와 data.evidence 위치를 참조한다.
`resolve_evidence_references(output, evidence_by_call)`는 output을 다시 검증하고,
현재 요청의 성공 검색 존재/0-based index 범위를 검사한다. 실패한 검색/다른 Tool/다른 요청의
ID/없는 근거를 인용할 수 없다. helper를 직접 쓸 때도 성공 검색 snapshot만 제공해야 한다.
이 검증은 의료적 함의, 환자 사실, 인용 문장과 논문 내용의 일치까지 증명하지 않는다.

서버용 `ResolvedEvidence`는 source_ref와 실제 evidence_type/title/pmid/doi/journal/
publication_year/study_type/organization/source_url/full_text_available만 원본에서 복사한다.
없던 metadata는 null 그대로 유지하고 abstract/ai_summary/전체 환자 기록은 반환하지 않는다.
LLM이 이 metadata를 반환하는 schema는 없다. 이 클래스와 StructuredAgentResult는 서버용이며
LLM 최종 출력 모델은 ChatAnswerV1/FeedAnswerV1뿐이다.

| AI 콘텐츠 | 현재 Frontend 매핑 | 신뢰 가능한 서버 조립 |
|---|---|---|
| Chat `answer` | ChatMessage.text | message id/role은 Backend가 생성 |
| Chat `citation_refs` | ChatMessage.citations의 별도 출처 영역 | 서버가 원본 title/source_url/type을 조립. 표시 번호는 UI/Assembler 책임이며 answer 안 번호와 연결하지 않음. evidence_level mapping은 미확정 |
| Feed `headline` | FeedItem.title | AI 헤드라인과 원본 논문 title을 구분 |
| Feed `summary_bullets` | FeedItem.summary_bullets | 그대로 |
| Feed `personal_reason` | FeedItem.personal_reason | 실제 제공된 patient/care context와 근거에 한정 |
| Feed `category` | FeedItem.category | 단일 category 유지 |
| Feed `source_ref` | metadata lookup | id/journal/published_at/evidence_level/read_minutes/is_bookmarked는 LLM 생성 금지 |
| Paper `summary_bullets/body/personal_meaning/limitations/glossary` | PaperDetail의 같은 필드 | body 문단은 text만 제공; nullable personal_meaning 표시 정책은 별도 |
| Paper `source_ref` | metadata lookup | id/원 제목/journal/date/authors/DOI/pubmed_url/study_type/evidence_level은 신뢰 가능한 source에서 조립 |

Assembler/Frontend DTO 생성은 구현하지 않았다. 현재 RAG는 DB paper UUID, authors,
정확한 published_at 날짜, evidence_level을 반환하지 않는다. publication_year만으로 날짜를
만들거나 PMID를 UUID로 간주하지 않는다. 현재 Frontend의 필수 metadata를 모두 채울 수는
없으며, 누락 표현 및 DB ID 매핑은 후속 합의가 필요하다. Citation source_url이 null인 경우도
가짜 URL을 생성하지 않는다. Structured Chat의 answer에는 inline 인용 번호를 생성하지
않도록 안내하며, citation_refs만 authoritative 출처 연결로 사용한다. 기존 mock의 [1] 같은
본문 표기를 근거로 서버가 번호를 신뢰하지 않는다. 모델이 정책을 무시하고 번호를 출력해도
일반 answer 문자열일 뿐 출처 연결로 해석하지 않는다. 별도 marker validator는 도입하지 않았다.

정책은 relevance_score를 검색 유사도로만 사용하고 근거 등급/의학적 확신도로 해석하지 않는다.
근거 없는 인용·환자 사실·개인화 설명을 만들지 않고, 빈 검색을 효과 없음/안전함으로 해석하지
않도록 안내한다. new_research와 guideline을 구분하고, 현재 1..6 → A/B/C/D/guideline의
미확정 변환을 LLM이 결정하지 않게 한다. disclaimer는 현재 UI 고정 책임이다.

### V2 확장 및 남은 합의

V1에는 easy_summary/finding/comparison/limitation/복수 category를 넣지 않았다.
팀 합의 후 FeedAnswerV2/PaperDetailContentV2와 명시적 schema_version="2"를 정의하고,
서버가 허용 버전을 선택하며 소비자도 해당 버전을 지원하도록 한다. extra 금지인 V1에
필드를 조용히 추가하거나 V2 payload를 V1로 강제 파싱하지 않는다.
paper_analysis의 summary_finding/summary_comparison/summary_limitation은 그대로이며,
easy_summary migration이나 새 dependency는 없다.

다음 Tool Contract 구체화는 진행할 수 있다. 실제 연결 전에는 개인화 Context 출처,
등급 변환, metadata 누락 정책/ID 매핑, 순위·최신성 기준, 문장 수준 근거 검증,
Paper Detail 문단별 인용, nullable 개인화 UI 처리가 추가 합의 대상이다.
production Registry는 계속 비어 있고 Provider native schema 연동도 후속 선택 사항이다.

오프라인 검증 (`backend/`; live Provider test를 명시적으로 끈다):

```bash
MINDCARE_RUN_PROVIDER_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s app/ai/agent/tests -t . -v
MINDCARE_RUN_PROVIDER_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s . -t . -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m app.ai.agent.testing.demo
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m app.ai.agent.testing.structured_demo
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c 'import app.ai.agent.agent_loop_runner; import app.ai.agent.outputs; import app.ai.retrieval.search; import app.ai.retrieval.evidence'
git diff --check
```
