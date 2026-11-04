# 최소 로그인 기능과 확인 방법

2026-11-02 구현 범위: **기존 계정 로그인 → 본인 프로필·피드·설정 이용 → 로그아웃**.
회원가입·갱신 토큰·자동 로그인 연장·프론트 화면은 후속 작업이다. 토큰 만료 시 다시 로그인한다.
기존 `011_authentication.sql`을 그대로 사용하며 추가 마이그레이션은 없다.

검증 결과 (2026-11-02): 아래 오프라인·회귀 테스트 56개, PostgreSQL 통합 테스트 35개,
총 **91개 통과**. 점검 스크립트도 실제 DB를 연결한 TestClient로 검증했다.
실행 중인 외부 서버·실제 LLM 추천 호출은 이 자동 검증에 포함하지 않는다.

이후 사용자 제공 서버 실행 로그에서도 기본 점검 10개가 모두 통과했다.
재현 방법과 검증 범위·결과는 [AUTH_TEST_REPORT.md](AUTH_TEST_REPORT.md)에 기록했다.

## 1. 서버 준비

`backend/`에서 프로젝트 Python 환경을 활성화하고 실행한다.

```bash
python -m pip install -r requirements.txt
```

신규 의존성은 `PyJWT==2.15.1`이다. 비밀번호는 기존 `argon2-cffi`로 검증한다.
DB는 001~011 적용이 필요하다. 011이 아직 없으면 기존 절차로 한 번 적용한다.
기존 볼륨에서는 `docker compose up -d`만으로 새 마이그레이션이 적용되지 않는다.

```bash
docker exec -i mindcare-db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 --single-transaction < app/db/migrations/011_authentication.sql
```

`backend/.env`에 다음 인증 설정을 추가한다. 프로세스 환경 변수가 `.env`보다 우선한다.

```dotenv
AUTH_JWT_SECRET=<직접 생성한 32바이트 이상의 무작위 비밀키>
AUTH_JWT_ISSUER=mind-care
AUTH_JWT_AUDIENCE=mind-care-app
AUTH_ACCESS_TOKEN_MINUTES=15
```

키는 `python -c "import secrets; print(secrets.token_urlsafe(48))"`로 로컬에서 생성한다.
같은 서버의 모든 워커는 같은 키를 사용한다. 실제 키는 `.env`에만 저장하고 공유 문서·Git에 넣지 않는다.
키 누락·너무 짧은 키는 인증 요청에 **503**을 반환한다. 설정을 바꾼 뒤에는 서버를 재시작한다.

기존 테스트 계정에 이미 인증 정보를 연결했다면 그 비밀번호로 로그인한다.
아직 연결하지 않은 기존 간병인만 아래 스크립트로 준비한다. 기존 비밀번호를 덮어쓰지 않는다.

```bash
python -m scripts.seed_dev_auth --email caregiver@example.com
```

실제 이메일로 바꾸고 숨김 프롬프트에 비밀번호를 입력한다. 새 사용자를 생성하는 명령은 아니다.

개발용 프로필 시드를 사용한 환경의 이메일은 `dev-seed-1@example.com`부터
`dev-seed-4@example.com`까지다. 네 계정 모두 준비하려면 아래 명령에서 12~128자 비밀번호를 입력한다.
공통으로 배포하는 고정 비밀번호는 없다. 계정 자체가 없는 환경의 준비 방법은
`python -m scripts.seed_dev_profiles --help`를 참고한다.

```bash
python -m scripts.seed_dev_auth --all-dev-profiles
```

```bash
PRELOAD_EMBEDDING_MODEL=0 python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

## 2. Swagger로 확인

브라우저에서 `http://127.0.0.1:8000/docs`를 연다.

1. `POST /auth/login`의 **Try it out**에 테스트 이메일·비밀번호를 입력한다.
   JSON 형식은 `{"email":"caregiver@example.com","password":"직접 설정한 비밀번호"}`다.
2. **200** 응답의 `access_token`을 복사한다. `user.id`가 이후 경로에 쓸 사용자 ID다.
3. 상단 **Authorize**에 토큰 문자열만 붙여 넣는다. `Bearer ` 접두사는 Swagger가 붙인다.
4. `GET /auth/me`, `GET /feed/{user_id}`, `GET /users/{user_id}/feed-settings`가 **200**인지 확인한다.
5. 본인 `PATCH /users/{user_id}/feed-settings`에 `{"feed_refresh_days":5}`를 보내면 실제 설정이 바뀐다.
   확인 후 원래 값으로 돌린다. 범위 밖 값은 **422**다.
6. 경로의 사용자 ID를 다른 UUID로 바꾸면 **403**이다. 본인 경로에 다른 간병인의 환자 ID를 주면 **404**다.
7. `GET /recommendations/{user_id}?k=1`로 추천을 확인한다. 실제 검색·LLM이 실행될 수 있다.
   **200** 응답의 `mode`가 `ai` 또는 `search`인지 확인한다. `search`도 정상 폴백이다.
8. `POST /auth/logout`을 본문 없이 호출하면 **200**, `{"ok":true}`다.
9. 같은 토큰으로 `/auth/me`나 본인 프로필을 요청하면 **401**이다.
   토큰 만료 전 같은 로그아웃 요청을 반복하면 **200**이다.
10. Swagger의 Authorize에서 **Logout**한 뒤 보호 API를 호출해도 **401**이다.

로그아웃은 현재 로그인 세션만 폐기한다. 다른 기기·다른 로그인 세션은 유지한다.
만료·변조·서명 오류 토큰의 로그아웃 요청은 **401**이며 프론트는 로컬 토큰을 지우면 된다.

## 3. 터미널에서 서버 점검

서버를 실행한 상태에서 아래 명령을 사용한다. 이메일과 비밀번호를 대화형으로 받고 토큰은 출력하지 않는다.
로그인·내 정보·본인 프로필·설정 조회·미인증/타인 GET·타인 PATCH 차단·로그아웃을 순서대로 확인한다.
프로필·설정을 변경하지 않고 이번 점검에서 만든 로그인 세션을 폐기한다.

```bash
python -m scripts.check_auth --base-url http://127.0.0.1:8000
# 추천 응답까지 확인할 때 (검색·LLM 실행 가능)
python -m scripts.check_auth --base-url http://127.0.0.1:8000 --recommendations
```

## 4. 자동 테스트

DB·외부 API 없이 인증 및 관련 업무 회귀 테스트:

```bash
python -m unittest \
  tests.test_auth_security tests.test_auth_api \
  tests.test_feed_settings \
  tests.test_recommendations.OfflineFeedHandoffTests \
  tests.test_recommendations.FeedCacheTests \
  tests.test_feed_execution_dependency tests.test_dev_cors -v
```

실제 PostgreSQL 통합 테스트 (`DATABASE_URL` 연결, 스키마 생성 권한 필요):

```bash
python -m unittest tests.test_auth_db tests.test_seed_dev_auth tests.test_feed_settings_db -v
```

DB 테스트는 무작위 격리 스키마에서 실행하고 롤백한다. 기존 계정·환자·인증 정보에 쓰지 않는다.
인증 DB 테스트는 실제 마이그레이션으로 FK·UNIQUE를 생성한다.
계정 비활성화·비밀번호 변경·만료 경계·세션 소유·로그아웃 후 거부·다른 세션 유지도 확인한다.
기존 업무 회귀 테스트만 인증 의존성을 대체하며, `test_auth_*`는 실제 인증 검사를 실행한다.

## 5. 프론트 전달 계약

| API | 요청 | 성공 응답 |
|---|---|---|
| `POST /auth/login` | JSON `email`, `password` | `access_token`, `token_type:"bearer"`, `expires_in`(남은 초), `expires_at`(UTC), `user` |
| `GET /auth/me` | `Authorization: Bearer <access_token>` | `id`, `name`, `email`, `feed_refresh_days` |
| `POST /auth/logout` | 같은 Authorization, 본문 없음 | `{"ok":true}` |

- `user`는 `/auth/me`와 같은 공개 필드다. 비밀번호·토큰 해시는 반환하지 않는다.
- 보호 대상: `/feed/{user_id}`, `/feed_user/{user_id}`, `/feed_patient/{user_id}/{patient_id}`,
  `/recommendations/{user_id}`, `GET/PATCH /users/{user_id}/feed-settings`.
- 로그인 응답의 `user.id`로 기존 `DEV_USER_ID`를 대체하고, 보호 요청마다 Authorization 헤더를 추가한다.
- 현재 프론트 `src/api/client.ts`는 GET/PATCH만 허용하므로 POST와 Authorization 전달도 추가해야 한다.
  화면 및 클라이언트 코드는 이번 작업에서 구현하지 않았다. 연결 전 기존 프론트의 개인화 요청은 401이다.
- 앱 재실행 시 안전하게 보관한 토큰으로 `/auth/me`를 확인한다. **401이면 토큰·사용자별 화면 캐시를 지우고 로그인 화면**으로 이동한다.
  자동 갱신 API는 없으며, 503·네트워크 오류를 로그아웃으로 처리하지 않는다.
- 로그아웃 API 성공 또는 401 확인 후 로컬 토큰과 사용자별 캐시를 지운다. 네트워크 실패 시 서버 세션 폐기 여부는 확인할 수 없다.
- **403** 본인 경로 불일치, **404** 담당 환자 없음, **422** 입력 오류,
  **429** 로그인 제한(`Retry-After: 60`), **503** 인증 설정/DB 장애.
- 로그인 실패의 없는 계정·비밀번호 오류·비활성 계정은 동일한 401 메시지다.
- 인증 응답은 `Cache-Control: no-store`. 검증 오류에 비밀번호 원문을 포함하지 않는다.
- `/health`와 `/papers` 조회는 공개 API다. 개발용 웹 CORS는 기존 localhost:8081 출처 정책을 따른다.

## 6. 현재 범위와 후속 작업

- 액세스 JWT는 HS256, 기본 최대 15분(설정 1~60분). 매 보호 요청에서 DB 세션·계정 상태도 확인한다.
  **로그인 시점부터 계산**하며 사용·새로고침으로 연장되지 않는다. 60으로 바꿔도 고정 60분이다.
  설정 변경은 서버 재시작 후 새로 발급하는 토큰에 적용된다. 기존 토큰의 만료 시각은 바뀌지 않는다.
- 회원가입, 갱신 토큰 발급·교체, 이메일 확인·비밀번호 재설정, 세션 정리 배치는 후속 작업이다.
  `auth_refresh_tokens` 테이블은 이번 로그인 흐름에서 사용하지 않는다.
- 로그인 요청 제한은 **프로세스별 클라이언트 IP당 60초 동안 10회**이며 성공·실패 모두 센다.
  프로세스 재시작 시 초기화된다. 다중 워커/공개 서비스에서는 공유 저장소나 프록시의 제한을 추가해야 한다.
  앱은 임의의 `X-Forwarded-For`를 직접 신뢰하지 않는다. 프록시의 신뢰 IP 설정에 따라 관측되는 IP가 달라진다.
- 외부 서버에 연결할 때는 HTTPS를 사용한다. 모바일의 `127.0.0.1`은 휴대폰 자신이므로 개발 서버 주소를 사용한다.
- JWT 라이브러리 검증 기준: [PyJWT 공식 사용 문서](https://pyjwt.readthedocs.io/en/stable/usage.html).
