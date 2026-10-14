# AI·검색 (김현서·허웅 영역)

> 담당: **김현서**(검색·임베딩·근거 등급·가이드라인 조각 나누기), **허웅**(에이전트·FeedWorkflow).
> AI 쪽 상세 설계는 AI 담당 문서가 정본이다. 이 파일은 안내만 한다.

- 에이전트·FeedWorkflow·ChatWorkflow 설계와 Production 조립 예시: [`backend/app/ai/agent/README.md`](../backend/app/ai/agent/README.md)
- 백엔드에서 AI를 부르는 쪽(`/recommendations`의 `mode="ai"`, 폴백 규칙, 교체 지점 `get_feed_execution()`): [`api.md`](api.md)
- AI가 쓰는 DB 함수(`search_similar()`, `get_papers_by_ids()`, 임베딩·근거 등급·가이드라인 저장, `feed_read.py`): [`db.md`](db.md)
- 근거 등급 변환 규칙의 정본: `backend/app/ai/retrieval/evidence.py`의 `to_grade()`
