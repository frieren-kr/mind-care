"""합성 고정 fixture만 사용하는 3개 Fake. 저장/의료 판단/DB/API 호출 없음."""
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import AgentContext
from ..tools import contracts as c

FIXTURE_NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)

# 전부 합성 patient_care 기록이다. UUID는 내부 tie-break만을 위한 값이다.
_FAKE_CARE_LOG_ROWS = (
    (UUID(int=4), FIXTURE_NOW - timedelta(hours=2), "[FAKE] 합성 관찰 동시각 A", None),
    (UUID(int=3), FIXTURE_NOW - timedelta(hours=25), "[FAKE] 새벽에 여러 차례 잠에서 깸", None),
    (UUID(int=5), FIXTURE_NOW - timedelta(hours=2), "[FAKE] 합성 관찰 동시각 B", "합성 태그"),
    (UUID(int=9), FIXTURE_NOW - timedelta(hours=1), "[FAKE] 합성 관찰 최근", " 차분 "),
    (UUID(int=6), FIXTURE_NOW - timedelta(days=14), None, None),
    (UUID(int=7), FIXTURE_NOW - timedelta(days=15), "[FAKE] 이전 범위 관찰", None),
    (UUID(int=8), FIXTURE_NOW - timedelta(days=91), "[FAKE] 최대 범위 밖 관찰", None),
    (UUID(int=10), FIXTURE_NOW, "[FAKE] 종료 경계 관찰", None),
    (UUID(int=11), FIXTURE_NOW + timedelta(hours=1), "[FAKE] 종료 이후 관찰", None),
)


def fake_get_patient_profile(context: AgentContext, args: c.PatientProfileInput) -> c.PatientProfileOutput:
    return c.PatientProfileOutput(
        name="합성 테스트 환자", dementia_stage="경도",
        diagnosis_date=date(2025, 1, 1), symptoms=["수면 변화"], interests=["수면"],
    )


def fake_get_recent_care_logs(context: AgentContext, args: c.RecentCareLogsInput) -> c.RecentCareLogsOutput:
    # 실제 DB 권한 검증은 하지 않는다. 요청당 고정된 end_at과 immutable fixture만 사용한다.
    end = FIXTURE_NOW
    start = end - timedelta(days=args.days)
    rows = [row for row in _FAKE_CARE_LOG_ROWS if start <= row[1] < end]
    rows.sort(key=lambda row: (row[1], row[0]), reverse=True)
    logs = [c.CareLogItem(logged_at=timestamp, content=content, mood_tag=mood)
            for _, timestamp, content, mood in rows[:args.limit]]
    return c.RecentCareLogsOutput(period=c.RecentCareLogsPeriod(start_at=start, end_at=end),
                                 logs=logs, total_count=len(rows))


def fake_search_evidence(context: AgentContext, args: c.EvidenceSearchInput) -> c.EvidencePackage:
    return c.EvidencePackage(query=args.query, evidence=[c.EvidenceItem(
        evidence_type="new_research", pmid="FAKE-PMID", title="[FAKE] Dementia sleep orchestration fixture",
        journal="Fake Journal", doi="FAKE-DOI", publication_year=2025, full_text_available=False,
        study_type="systematic_review", relevance_score=0.94,
        ai_summary="[FAKE] 수면 변화 관련 합성 근거", abstract="Fake abstract; not clinical evidence",
    )][:args.top_k])


def fake_registry() -> ToolRegistry:
    handlers = {
        ToolName.PATIENT_PROFILE: fake_get_patient_profile,
        ToolName.RECENT_CARE_LOGS: fake_get_recent_care_logs,
        ToolName.SEARCH_EVIDENCE: fake_search_evidence,
    }
    return ToolRegistry(tuple(ToolSpec(TOOL_CONTRACTS[name], handler, test_only=True)
                              for name, handler in handlers.items()), mode="test")
