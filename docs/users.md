# 사용자·환자 (김한슬 영역)

> 담당: **김한슬**. 이 문서는 루트 `AGENTS.md`에 있던 문장을 그대로 옮긴 것이다 — 고칠 때는 담당자가 고친다.
> API 주소 표는 [`api.md`](api.md), 피드 개인화 조회(`feed_read.py`, 박주현)는 [`db.md`](db.md).

- 담당 테이블: `users`, `patient_profiles`, `caregiver_profiles`, `clinical_assessments`, `safety_events`, `medications`, `medical_visits`
- `/feed…` 주소 3개(담당: 김한슬)는 **간병인·환자 정보 조회(피드·챗봇 개인화 입력용)**다. 이름은 feed지만
  논문을 돌려주지 않는다 — **논문 추천은 `/recommendations`**. 응답 형식의 정본은 `backend/app/user_schemas.py`.
- 사용자·환자 함수 목록은 `app/db/user_functions.py` 참고 (담당: 김한슬).

## 사용자별 피드 갱신 주기

새 코드를 실행하기 전에 `010_user_feed_refresh_days.sql`을 적용한다. 기존 DB에서는 backend/에서 실행:

```bash
docker exec -i mindcare-db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 --single-transaction < app/db/migrations/010_user_feed_refresh_days.sql
```

- `users.feed_refresh_days`: 정수 1~7일, NOT NULL, 기본 3일. 간병인 자가점검과 별개인 계정 설정이다.
- 컬럼을 처음 추가할 때 기존 사용자는 3일로 설정된다. 재실행 시 저장된 설정값을 유지하며, 새 사용자는 DB 기본값을 쓴다.
- `UserOut`, `UserSummary`, `CaregiverContext`, `UserProfileContext`에 `feed_refresh_days`를 포함한다.
  `/feed/{user_id}`와 `/feed_user/{user_id}`에서는 최상위 필드로 반환한다.
- 조회: `GET /users/{user_id}/feed-settings`.
- 변경: `PATCH /users/{user_id}/feed-settings`, 본문 `{"feed_refresh_days": 5}`.
  응답은 `{"user_id": "UUID", "feed_refresh_days": 5}`. 정수 1~7만 허용하고 null·불리언·문자열·소수·누락·추가 필드는 422,
  없는 사용자는 404, 잘못된 UUID 형식은 422다. 다른 계정 값은 유지하고 `users.updated_at`을 갱신한다.
- 설정 변경은 피드를 즉시 생성하지 않는다. 다음 AI 피드 요청부터 기존 생성 시각에 새 기간을 적용한다.
  기간을 줄여 기존 피드가 만료되면 다음 요청에서 재생성한다. `refresh=true`는 기간과 무관하게 재생성한다.
- 검색 피드는 현재 저장·캐시하지 않으며 AI 실행기 기본값도 미연결이다. 설정을 저장해도 검색 경로의 동작은 그대로다.
  요청이 없어도 정기적으로 생성하는 스케줄러는 이 기능에 포함하지 않는다.

| 데이터 접근 함수 | 입력 | 반환 |
|---|---|---|
| `get_user_feed_settings(user_id)` | UUID 또는 UUID 문자열 | `FeedSettingsOut` 또는 None(없는 사용자·잘못된 UUID) |
| `update_user_feed_settings(user_id, data)` | UUID 또는 UUID 문자열, `FeedSettingsUpdate` 또는 dict(`feed_refresh_days` 필수) | 저장된 `FeedSettingsOut` 또는 None. 잘못된 설정은 DB 변경 전에 ValueError |

검증: `python -m unittest tests.test_feed_settings tests.test_recommendations.FeedCacheTests`.


## 진료·복약 날짜 무결성 (010 확장)

- 요청에 따라 기존 `010_user_feed_refresh_days.sql`에 DB 트리거를 추가했다. 이미 010을 적용한 DB도
  위 `--single-transaction` 명령으로 다시 실행해야 한다. 컬럼·제약·트리거가 중복 생성되지 않고 갱신 주기 값은 유지된다.
- `medical_visits`: 방문 완료(`is_visited=true`)인데 `visit_date`가 미래면 저장·수정을 거부한다.
- `medications`: 복용 중단(`is_taking=false`)인데 `end_date`가 미래면 저장·수정을 거부한다.
- 한국 시간 기준 SQL 문장 시작일로 판단한다. 세션 시간대가 달라도 결과가 같으며 오늘 날짜는 허용한다.
  미래 진료 예약, 복용 중인 약의 미래 종료일, 종료일을 모르는 중단 기록(`end_date=null`)은 기존 정책대로 허용한다.
- 애플리케이션 검증은 유지하고 DB에서도 모든 INSERT/UPDATE의 최종 행을 검사한다. 직접 SQL·부분 수정도 차단한다.
  위반 시 SQLSTATE `23514`(CheckViolation), 제약 이름은 `medical_visits_completed_date_check` /
  `medications_stopped_end_date_check`다.
- 적용 중 두 테이블의 쓰기를 잠그고 기존 모순 기록을 검사한다. 발견되면 적용을 실패시키며 기록을 자동 수정하지 않는다.
- 검증: `python -m unittest tests.test_feed_settings_db` — 격리 스키마에서 마이그레이션·직접 SQL·부분 수정·재실행을 검증한 뒤 롤백한다.
