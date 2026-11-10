# 사용자·환자 (김한슬 영역)

> 담당: **김한슬**. 이 문서는 루트 `AGENTS.md`에 있던 문장을 그대로 옮긴 것이다 — 고칠 때는 담당자가 고친다.
> API 주소 표는 [`api.md`](api.md), 피드 개인화 조회(`feed_read.py`, 박주현)는 [`db.md`](db.md).

## 기존 계정 로그인 (2026-11-02)

- `011_authentication.sql`의 `user_credentials`에 인증 정보가 연결된 기존 계정으로 로그인한다.
  사용자·환자 관계는 유지한다. 인증 정보가 없는 계정이나 비활성 계정은 로그인할 수 없다.
- `POST /auth/login`, `GET /auth/me`, `POST /auth/logout`을 제공한다.
  `user.id`가 기존 개인화 경로의 `user_id`다. 회원가입·토큰 자동 갱신은 후속 범위다.
- `/feed…`, `/recommendations`, 피드 설정 조회·수정은 로그인 및 본인 확인 뒤 실행한다.
  토큰 누락·만료·폐기는 401, 다른 사용자 ID는 403, 본인 경로의 담당 외 환자는 404다.
  아래 기존 404·422 설명은 인증·본인 확인을 통과한 요청 기준이다.
- 세션은 기본 최대 15분이며 매 요청에서 활성 상태·만료·폐기·소유·비밀번호 변경 시각을 확인한다.
  로그아웃은 현재 세션만 폐기한다. 이때 이미 발급된 토큰도 다음 요청부터 거부한다.
- 공개 인증 모델은 `app/auth_schemas.py`, DB 관문은 `app/db/functions.py`의 인증 함수다.
  상세 계약은 [`db.md`](db.md), 실행·프론트 연결 방법은 [`AUTH_CHECK.md`](../backend/AUTH_CHECK.md) 참고.
- 검증: `python -m unittest tests.test_auth_security tests.test_auth_api tests.test_auth_db`.

- 담당 테이블: `users`, `patient_profiles`, `caregiver_profiles`, `clinical_assessments`, `safety_events`, `medications`, `medical_visits`
- `/feed…` 주소 3개(담당: 김한슬)는 **간병인·환자 정보 조회(피드·챗봇 개인화 입력용)**다. 이름은 feed지만
  논문을 돌려주지 않는다 — **논문 추천은 `/recommendations`**. 응답 형식의 정본은 `backend/app/user_schemas.py`.
- 사용자·환자 함수 목록은 `app/db/user_functions.py` 참고 (담당: 김한슬).

## 챗봇 최근 돌봄 기록 조회 (2026-11-10)

- 공용 관문 `app/db/functions.py`의 `get_recent_care_logs()`를 추가했다. 구현은 `user_functions.py`다.
  입력·반환 계약은 [`db.md`](db.md)의 챗봇 최근 돌봄 기록 표를 따른다.
- 기존 일반 사용자 조회 함수와 달리, 이 함수는 **환자 소속을 직접 확인**한다. 확인·건수·목록 조회는
  하나의 읽기 전용 REPEATABLE READ 트랜잭션이다. 권한 실패·조회 장애는 빈 성공으로 반환하지 않는다.
- 사용자·환자가 일치하는 `patient_care` 기록만 `[start_at, end_at)`에 포함하며
  `logged_at DESC, id DESC` 정렬, limit 적용 전 전체 건수, 원문·null 보존을 보장한다.
- `app/api/chat.py`의 요청별 `build_chat_tool_registry()`에서 기존 어댑터에 DB 함수를 주입한다.
  환자를 선택한 요청에서만 도구가 노출된다. 기본 최근 14일·10건이며 최대 90일·30건 정책은 기존 챗봇과 같다.
- `tests/test_recent_care_logs_db.py`는 테스트 전용 스키마에서 실제 조회·권한·기간 경계·동시 변경·읽기 전용 제약과
  운영 챗봇 도구 연결을 검증한 뒤 스키마를 삭제한다. public 데이터는 수정하지 않는다.
- 검증: `python -m unittest tests.test_recent_care_logs_db tests.test_chat`.
- Windows에서 인증 테스트 SQL을 읽을 때 발생하는 `UnicodeDecodeError`는
  `tests/test_auth_db.py`의 두 `read_text()`에 `encoding="utf-8"`을 명시해 수정했다.
  검증: `python -m unittest tests.test_auth_db tests.test_chat_store_db`.

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
