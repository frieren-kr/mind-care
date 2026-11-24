# 서버 배포 (AWS EC2)

> 담당: **박주현**. 공통 규칙은 루트 [`AGENTS.md`](../AGENTS.md), 배치 동작은 [`db.md`](db.md)의 "통합 배치".

EC2 한 대(Ubuntu 24.04, t3.medium 메모리 4GB + 스왑 4GB, Docker 설치됨)에 **DB + 백엔드 + 주기 배치 + 웹(nginx)**을 올린다.

| 파일 | 하는 일 |
|---|---|
| `backend/Dockerfile` | 백엔드 이미지 (python 3.12-slim, torch **CPU 전용** 2.14.1 — 로컬 `.venv`와 같은 버전) |
| `backend/.dockerignore` | `.env`·덤프·로그·캐시가 이미지에 들어가지 않게 |
| `deploy/docker-compose.prod.yml` | 서버용 compose. db(127.0.0.1:5432) + backend(127.0.0.1:8000) + web(80, 127.0.0.1:8443) |
| `deploy/web.Dockerfile` (+ `.dockerignore`) | 프론트 웹 빌드(`expo export`) → nginx 이미지 |
| `deploy/nginx-app.inc` | web nginx의 앱 부분: `/` 정적 파일(기본 인증), `/api/` → backend(기본 인증 없음, 로그인 토큰으로 보호 → 10번) |
| `deploy/nginx.conf` / `deploy/nginx.https.conf` | web nginx 1단계(HTTP 80) / 2단계(HTTPS 8443 + 80은 https로 이동). 고르는 법 → 11번 |
| `deploy/host-nginx/mindcare-stream.conf` | 서버 본체 nginx(stream): 443을 SSH·HTTPS로 나눈다 → 11번 |
| `deploy/certbot-deploy-hook.sh` | 인증서가 갱신되면 web nginx reload → 11번 |
| `deploy/run_update_papers.sh` | 실행 중인 backend 컨테이너 안에서 `update_papers` 배치를 돌린다 (cron용) |
| `deploy/run_summarize_chats.sh` | 같은 방식으로 `summarize_chats`(챗봇 대화 하루 요약 + 7일 지난 원문 삭제)를 돌린다 (cron용) |

compose 명령은 **항상 레포 루트에서, `--env-file backend/.env`를 붙여서** 쓴다. 아래에서는 줄여서 `$DC`로 적는다.

```bash
DC="docker compose --env-file backend/.env -f deploy/docker-compose.prod.yml"
```

이름 붙은 볼륨 (프로젝트 이름 `mindcare`): `mindcare_pgdata`(DB), `mindcare_models`(bge-m3 캐시),
`mindcare_logs`(배치 로그 `/app/logs`), `mindcare_state`(요약 실패 기록 `/app/state`).
컨테이너를 지우고 다시 만들어도 남는다. **`$DC down -v`는 DB까지 지우므로 쓰지 않는다.**

## 1. 서버 준비 (처음 한 번)

```bash
sudo timedatectl set-timezone Asia/Seoul    # cron이 한국 시간으로 돌게
sudo usermod -aG docker ubuntu              # sudo 없이 docker (cron도 ubuntu로 돈다). 다시 로그인해야 적용
git clone https://github.com/frieren-kr/mind-care.git
cd mind-care
git checkout develop                         # 배포할 브랜치
```

## 2. `backend/.env` 만들기

```bash
cp backend/.env.example backend/.env
chmod 600 backend/.env
nano backend/.env
```

채울 변수 (값은 레포·단톡·로그 어디에도 붙여 넣지 않는다):

- `POSTGRES_PASSWORD` — **로컬 기본값을 쓰지 말고 새로 정한다.** `openssl rand -hex 24`로 만든 값을 권장
  (영문·숫자만이라 접속 주소에 넣어도 깨지지 않는다).
- `DATABASE_URL` — 서버에서는 compose가 `db` 호스트 주소로 **덮어쓴다**. 비워 두거나 아무 값이어도 된다.
- `PUBMED_API_KEY`, `PUBMED_EMAIL` — 수집
- `OPENAI_SUMMARY_API_KEY`(없으면 `OPENAI_API_KEY`), `OPENAI_SUMMARY_MODEL` — 논문 요약·챗봇 대화 하루 요약
- `OPENAI_API_KEY`, `GEMINI_API_KEY` — 챗봇 등
- `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `PRELOAD_EMBEDDING_MODEL` — 로컬과 같게
- `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `APP_ENV`
- `EXPO_PUBLIC_DEV_USER_ID` — 웹 빌드용 시연 사용자 id. 처음엔 비워 두고 8-2에서 채운다 (비어 있으면 web 빌드만 실패)

## 3. 실행

```bash
$DC up -d --build
$DC ps                      # db가 healthy, backend가 running이면 OK
$DC logs -f backend         # "임베딩 모델 예열 완료"가 보일 때까지 (첫 기동은 bge-m3 약 2GB를 받느라 몇 분)
curl http://127.0.0.1:8000/docs -o /dev/null -w "%{http_code}\n"   # 200
```

- **첫 기동(빈 `pgdata` 볼륨)일 때만** 마이그레이션 `001`~`008`이 파일명 순서대로 자동 적용된다.
- db·backend 포트는 서버 안(`127.0.0.1`)에서만 열려 있다. 외부에는 web(nginx, 80)만 열린다 → 8번.

## 4. 데이터 옮기기 (로컬 DB → 서버)

옛 덤프(`mindcare_papers_bge-m3.dump`, `paper_analysis_*.sql`) 대신 **로컬 최신 DB에서 논문 관련 테이블만** 데이터로 뽑는다.

| 테이블 | 내용 |
|---|---|
| `papers` | 논문 |
| `paper_embeddings` | bge-m3 임베딩 |
| `paper_analysis` | 요약·한국어 제목 + 근거 등급(`study_type` 등) |

- 사용자·환자·피드·대화(`users`, `patient_profiles`, `caregiver_profiles`, `feed_items`, `care_logs`, …)는 개발용 시드라 **빼고**,
  `guideline_knowledge`는 아직 비어 있어 뺀다 (채워지면 `-t guideline_knowledge`를 더한다).
- 덤프에는 **스키마가 없다** → 서버는 3번의 첫 기동으로 표가 이미 만들어져 있어야 한다.
- 2026-10-15 로컬 기준: 논문 1004 / 임베딩 1004 / 분석 1004(요약 1003, 등급 1004), 덤프 약 6MB.
  빈 DB에 001~008 적용 → 복원까지 로컬에서 확인했다.

**① 로컬(Windows PowerShell)에서 뽑기** — 덤프는 레포 밖에 둔다.

```powershell
New-Item -ItemType Directory -Force C:\Users\PJH\mindcare-dumps
docker exec mindcare-db pg_dump -U mindcare -d mindcare -Fc --data-only -t papers -t paper_embeddings -t paper_analysis -f /tmp/papers.dump
docker cp mindcare-db:/tmp/papers.dump C:\Users\PJH\mindcare-dumps\mindcare_papers_20261015.dump
docker exec mindcare-db rm /tmp/papers.dump
```

**② 서버로 옮기기** (SSH 포트 443, 키 경로는 예시)

```powershell
ssh -p 443 -i C:\Users\PJH\.ssh\mindcare-ec2.pem ubuntu@<서버주소> "mkdir -p ~/mindcare-dumps"
scp -P 443 -i C:\Users\PJH\.ssh\mindcare-ec2.pem C:\Users\PJH\mindcare-dumps\mindcare_papers_20261015.dump ubuntu@<서버주소>:~/mindcare-dumps/
```

**③ 서버에서 복원** (레포 루트에서)

```bash
$DC cp ~/mindcare-dumps/mindcare_papers_20261015.dump db:/tmp/restore.dump
$DC exec -T db psql -U mindcare -d mindcare -c "TRUNCATE papers CASCADE;"   # 다시 복원할 때 대비. feed_items도 같이 비워진다
$DC exec -T db pg_restore -U mindcare -d mindcare --data-only --single-transaction /tmp/restore.dump
$DC exec -T db rm /tmp/restore.dump
$DC exec -T db psql -U mindcare -d mindcare -c "SELECT (SELECT count(*) FROM papers) papers, (SELECT count(*) FROM paper_embeddings) emb, (SELECT count(summary_finding) FROM paper_analysis) summarized;"
```

`--single-transaction`이라 중간에 실패하면 전부 되돌아간다. 다 끝나면 서버의 덤프 파일은 지운다 (`rm ~/mindcare-dumps/*.dump`).

## 5. 주기 실행 (cron)

먼저 손으로 한 번 돌려 본다 (종료 코드 0 성공 / 1 일부 실패 / 2 실행 불가):

```bash
bash deploy/run_update_papers.sh --skip-collect --limit 1; echo "exit $?"
```

`crontab -e`에 한 줄 (3일마다 새벽 4시, 1번에서 서버 시간대를 Asia/Seoul로 바꿨다는 전제):

```cron
0 4 */3 * * bash /home/ubuntu/mind-care/deploy/run_update_papers.sh >> /home/ubuntu/mindcare-cron.log 2>&1
```

`*/3`은 "매달 1·4·7…31일"이라 달이 바뀔 때(31일 → 1일)는 하루 만에 다시 돈다. 단계마다 처리 안 된 논문만 고르므로 문제는 없다.

**챗봇 대화 하루 요약** (`summarize_chats`) — 매일 새벽 2시 30분. 기본으로 **어제(한국 날짜)** 대화를 요약하고
7일 지난 원문을 지운다. 논문 갱신(4시)보다 먼저 끝나게 앞 시간에 둔다 (잠금 파일이 따로라 겹쳐도 막히지는 않지만
메모리를 같이 쓰지 않게). 먼저 손으로 확인:

```bash
bash deploy/run_summarize_chats.sh --dry-run; echo "exit $?"   # LLM은 부르지만 저장·삭제는 안 한다
```

```cron
30 2 * * * bash /home/ubuntu/mind-care/deploy/run_summarize_chats.sh >> /home/ubuntu/mindcare-cron.log 2>&1
```

- 하루를 빠뜨렸으면 그날만 다시: `bash deploy/run_summarize_chats.sh --date 2026-11-05` (같은 날은 덮어쓴다).
  단, 원문은 7일만 남으므로 7일 안에 돌려야 한다.
- 키나 `OPENAI_SUMMARY_MODEL`이 없으면 요약을 못 하므로 **원문도 지우지 않고** 종료 코드 2로 끝난다
  (로그에 `설정 누락으로 삭제 건너뜀`). 설정을 고친 뒤 빠진 날을 `--date`로 다시 돌린다.
  일부 사용자만 요약에 실패한 경우는 종료 코드 1이고 삭제는 그대로 한다.

## 6. 코드 업데이트

```bash
cd ~/mind-care
git pull
$DC up -d --build           # 이미지를 다시 만들고 backend만 새로 띄운다 (볼륨·DB 데이터는 그대로)
```

**새 마이그레이션(`009_…` 이후)은 자동 적용되지 않는다** (initdb는 첫 기동 때만). 직접 넣는다:

```bash
$DC exec -T db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 < backend/app/db/migrations/009_xxx.sql
```

## 7. 로그 확인

```bash
$DC logs --tail 100 backend                                   # API 서버
$DC exec backend ls -l logs                                   # 배치 로그 목록 (update_papers_·summarize_chats_YYYY-MM-DD.log)
$DC exec backend tail -n 50 logs/update_papers_2026-10-18.log
tail -n 50 ~/mindcare-cron.log                                # cron 자체 오류 (docker 권한 등)
free -h                                                       # 배치 중 메모리·스왑
```

## 메모리 (4GB + 스왑 4GB)

이미지 안에서 잰 값 (2026-10-15): bge-m3는 불러온 직후 약 **0.7GB**, 실제로 임베딩을 한 번 하면 약 **2.0GB**로 커진다.
배치는 `docker exec`로 **별도 파이썬 프로세스**라 백엔드가 이미 올린 모델을 같이 쓰지 못하고 자기 몫을 또 올린다.

- 백엔드(검색을 한 번이라도 했으면 약 2GB) + 배치(약 2GB) + PostgreSQL·OS(수백 MB) ≈ **4.5GB 안팎** → RAM을 조금 넘어 스왑을 쓴다.
- 스왑까지 8GB라 OOM으로 죽을 가능성은 낮지만, 그동안 느려지고 끝난 뒤 백엔드 첫 응답이 느릴 수 있다.
- 3일 치 새 논문은 수십 편 수준이라 임베딩은 금방 끝나고, 새벽 4시라 사용자 영향도 작다 → **지금 구성으로 시작하고 첫 실행 때 `free -h`로 확인**한다.
- 문제가 되면: 배치 동안 백엔드를 잠시 내리거나(`$DC stop backend` → `$DC run --rm backend python -m scripts.update_papers …` → `$DC start backend`, 몇 분 중단),
  인스턴스를 t3.large(8GB)로 올린다.

## 8. 웹 공개 (nginx, 주소 하나)

`http://서버IP/` → 프론트 웹(정적 파일), `http://서버IP/api/...` → backend(접두사 `/api`를 떼고 전달).
**화면(`/`)에 HTTP 기본 인증**(팀 공용 아이디·비밀번호 하나)이 걸린다. db·backend 포트는 그대로 `127.0.0.1`만.
로그인 반영(10번) 뒤에는 `/api/`에 기본 인증이 없고, 개인화 API는 로그인 토큰(Bearer)으로 막는다.

### 8-1. AWS 설정 (처음 한 번)

- **탄력적 IP(Elastic IP)**를 할당해 인스턴스에 연결한다. 안 하면 인스턴스를 껐다 켤 때마다 IP가 바뀐다.
- 보안 그룹 인바운드에 **HTTP 80 (0.0.0.0/0)**을 추가한다. 5432·8000은 열지 않는다 (SSH 22는 기존대로).

### 8-2. 기본 인증 파일과 시연 사용자 id (서버에서, 레포에 올리지 않음)

```bash
cd ~/mind-care
# 기본 인증 아이디·비밀번호 (bcrypt). 실제 값은 단톡으로만 공유한다
docker run --rm httpd:2.4-alpine htpasswd -nbB 아이디 비밀번호 > deploy/htpasswd
chmod 644 deploy/htpasswd    # nginx(컨테이너 안 다른 사용자)가 읽을 수 있게. 내용은 해시라 원문 비밀번호는 없다

# 웹 빌드 인자: 시연에 쓸 사용자 id (users.id). 로그인 기능 전까지 웹 전체가 이 사용자로 동작한다
$DC exec backend python -m scripts.list_users     # id 확인 (backend가 떠 있어야 함)
nano backend/.env                                  # EXPO_PUBLIC_DEV_USER_ID=여기에-id 한 줄 추가
```

- `deploy/htpasswd`는 `.gitignore`에 있어 레포에 올라가지 않는다.
- `deploy/htpasswd`가 **없는 채로 띄우면** docker가 그 이름으로 빈 폴더를 만들어 nginx가 뜨지 않는다.
  그랬다면 `rmdir deploy/htpasswd` 후 위 명령으로 다시 만든다.
- `EXPO_PUBLIC_DEV_USER_ID`가 비어 있으면 web 빌드가 일부러 실패한다.
- `EXPO_PUBLIC_API_BASE_URL`은 기본값 `/api`(같은 주소)라 넣지 않는다.

### 8-3. 빌드·실행

```bash
$DC up -d --build            # db·backend는 바뀐 게 없으면 그대로, web만 새로 만든다
$DC ps                       # web이 running
curl -s -o /dev/null -w "%{http_code}
" http://127.0.0.1/                       # 401 (인증 필요)
curl -s -o /dev/null -w "%{http_code}
" -u 아이디:비밀번호 http://127.0.0.1/api/docs  # 404 (Swagger 비공개, 아래)
```

브라우저에서 `http://탄력적IP/`로 열고 아이디·비밀번호를 넣는다.

### 8-4. 업데이트 반영

- **프론트 코드가 바뀌었을 때**: `git pull` → `$DC up -d --build web`.
  `EXPO_PUBLIC_*` 값은 **빌드할 때 번들에 박히므로**, `backend/.env`의 `EXPO_PUBLIC_DEV_USER_ID`를 바꿨을 때도 다시 빌드해야 한다.
- **`deploy/nginx*.conf`·`nginx-app.inc`만 바뀌었을 때**: 볼륨으로 붙어 있어 빌드 없이 `$DC restart web`.
  (`backend/.env`의 `WEB_NGINX_CONF`를 바꿨을 때는 붙이는 파일이 바뀌므로 `$DC up -d web` → 11번)
- **비밀번호 변경**: `htpasswd` 파일을 다시 만들고 `$DC restart web`.
- 브라우저가 옛 화면을 보이면 강력 새로고침(Ctrl+Shift+R). JS·CSS 파일은 이름에 해시가 붙어 캐시 문제는 거의 없다.

### 8-5. Swagger(`/docs`)는 공개하지 않는다

`nginx.conf`가 `/api/docs`·`/api/redoc`·`/api/openapi.json`을 404로 막는다 (API 전체 목록을 밖에 보이지 않게).
필요할 때는 내 PC에서 **SSH 터널**로 본다:

```bash
ssh -i 키.pem -L 8000:127.0.0.1:8000 ubuntu@탄력적IP
# 연결해 둔 채로 브라우저에서 http://localhost:8000/docs
```

### 주의

- **HTTP라 기본 인증 비밀번호가 암호화되지 않고 오간다.** 공용 와이파이 등에서는 엿볼 수 있으니
  다른 곳에 쓰는 비밀번호를 재사용하지 말고, 시연용으로만 쓴다. 오래 공개할 거면 도메인 + HTTPS(Let's Encrypt)를 붙인다.
- 기본 인증을 통과한 사람은 모두 **같은 시연 사용자**로 보인다 (기록·대화가 섞인다).
- AI 피드처럼 오래 걸리는 요청을 위해 `/api/`는 응답을 120초까지 기다린다. 그보다 길면 504.

## 9. 지난 기간 채우기 (2024~2025년 논문)

평소 cron은 최근 7일만 모은다. 지난 기간은 `--from-date`·`--to-date`로 한 번 따로 돌린다.
수집은 **한 달씩** PubMed에서 받아 달마다 저장하고(이미 있는 논문은 건너뜀), 그 뒤 임베딩 → 근거 등급 → 요약은 평소 단계를 그대로 쓴다.
요약은 `--limit` 편수까지만 하므로 채우기 때는 크게 준다.

### 9-1. 먼저 받아만 보기 (선택, 몇 분)

```bash
cd ~/mind-care
bash deploy/run_update_papers.sh --only collect --from-date 2024-01-01 --to-date 2025-12-31 --dry-run
```

### 9-2. 백그라운드로 돌리기 (SSH를 끊어도 계속)

```bash
cd ~/mind-care
nohup bash deploy/run_update_papers.sh --from-date 2024-01-01 --to-date 2025-12-31 --limit 3000 \
    > ~/mindcare-backfill.log 2>&1 &
```

- 반드시 `run_update_papers.sh`(실행 중인 backend 컨테이너 안에서 실행)로 돌린다. 그래야 cron과 **같은 잠금 파일**을 써서
  둘이 겹치지 않는다. 채우기 도중 cron 시각이 오면 cron 쪽이 "이미 실행 중"을 남기고 건너뛴다.
  cron은 최근 7일을 보므로 다음 회차(3일 뒤)에 빠진 날을 채운다.
- **도는 동안 `$DC up -d --build`나 backend 재시작을 하지 않는다** (컨테이너 안의 배치도 같이 끝난다).
- 끊겼으면 같은 명령을 다시 치면 된다. 수집은 중복을 건너뛰고, 임베딩·등급·요약은 아직 처리 안 된 논문만 고른다.
  (수집은 처음 달부터 다시 받지만 PubMed 요청만 다시 할 뿐 몇 분이다.)

### 9-3. 진행 상황 보기

```bash
tail -f ~/mindcare-backfill.log        # 단계별 "[수집] 처리 n건", 요약은 "[번호/전체] 한국어 제목"이 한 줄씩
$DC exec -T db psql -U mindcare -d mindcare -c "SELECT
  (SELECT count(*) FROM papers) papers,
  (SELECT count(*) FROM paper_embeddings) emb,
  (SELECT count(summary_finding) FROM paper_analysis) summarized;"
free -h                                 # 임베딩 중 메모리·스왑
```

끝나면 로그 마지막에 `끝 … 종료 코드 N`이 찍힌다 (0 성공, 1 일부 실패 — 실패한 요약은 다음 실행·cron이 다시 시도).

## 10. 로그인(JWT) 서버 반영

**로그인 화면이 붙은 프론트와 같이 올린다.** 백엔드만 먼저 올리면 지금 웹(토큰 없이 `EXPO_PUBLIC_DEV_USER_ID`로 동작)의
홈·피드·내 정보·챗봇이 모두 401이 된다. API 동작·프론트 계약은 [`backend/AUTH_CHECK.md`](../backend/AUTH_CHECK.md).

이번에 바뀌는 배포 설정:

- `nginx.conf`: `/api/`는 기본 인증을 끄고(`auth_basic off`) 앱이 보낸 `Authorization: Bearer …`를 그대로 backend에 넘긴다.
  기본 인증과 Bearer가 같은 `Authorization` 헤더를 써서 둘을 같이 걸 수 없다. 화면(`/`) 기본 인증과 `/api/docs` 차단은 그대로.
- 로그인 횟수 제한(IP당 1분 10회)이 **사용자 IP 기준**으로 돌게, nginx가 `X-Forwarded-For`·`X-Real-IP`에 실제 접속 IP를 넣고
  backend uvicorn은 **web 컨테이너(고정 IP `10.231.0.10`)가 보낸 값만** 믿는다 (`--proxy-headers --forwarded-allow-ips`).
  그래서 compose 기본 네트워크 대역을 `10.231.0.0/24`로 고정했다. 서버에 이 대역을 쓰는 네트워크가 이미 있으면
  `docker-compose.prod.yml`의 대역·IP·`--forwarded-allow-ips`를 같이 바꾼다.

### 10-1. `backend/.env`에 추가 (값은 레포·단톡·로그에 붙여 넣지 않는다)

- `AUTH_JWT_SECRET` — **필수**, 32바이트 이상 무작위 값. 서버에서 `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`로 만든다.
  없거나 짧으면 로그인·보호 API가 503이다.
- `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`, `AUTH_ACCESS_TOKEN_MINUTES` — 선택 (기본값 있음, 토큰 유효 1~60분)
- `APP_ENV` — 시연 계정 스크립트(`seed_dev_auth`)는 `development` 또는 `test`일 때만 돈다.

### 10-2. 순서 (레포 루트에서)

```bash
cd ~/mind-care
git pull
$DC down                     # 네트워크 대역이 바뀌어 한 번 내렸다 올린다. -v 는 절대 붙이지 않는다 (DB 삭제)
$DC up -d db                 # DB만 먼저
$DC exec -T db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 --single-transaction \
    < backend/app/db/migrations/011_authentication.sql
$DC up -d --build            # 이미지 재빌드 (requirements에 PyJWT·argon2-cffi 추가) + backend·web 기동
$DC ps
```

- 011은 대소문자·앞뒤 공백만 다른 같은 이메일이 있으면 전체가 중단된다 (정리 후 다시 실행). 다시 실행해도 안전하다.
- 반영 중 cron 배치(5번)·채우기(9번)가 돌고 있으면 `down`이 같이 끝낸다. 배치가 없는 시간에 한다.

### 10-3. 시연 계정 만들기

기존 시연 사용자(`dev-seed-1~4@example.com`)에 비밀번호만 연결한다. 새 사용자를 만들지 않고 환자·기록은 그대로다.
비밀번호는 숨김 입력(12~128자)이라 `-T` 없이 실행한다. 정한 비밀번호는 단톡으로만 공유한다.

```bash
$DC exec backend python -m scripts.seed_dev_auth --all-dev-profiles --dry-run   # 저장 후 되돌림 (점검)
$DC exec backend python -m scripts.seed_dev_auth --all-dev-profiles
# 한 명만: $DC exec backend python -m scripts.seed_dev_auth --email dev-seed-1@example.com
$DC exec -T backend python -m scripts.seed_demo_care_logs   # 시연 돌봄 기록(최근 14일, 환자당 8~12건). 재실행 시 이 스크립트 기록만 교체, --dry-run 가능
```

### 10-4. 확인

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/                       # 401 (화면 기본 인증 그대로)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/api/health             # 200 (API는 기본 인증 없음)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/api/feed/00000000-0000-0000-0000-000000000000  # 401 (토큰 필요)
$DC exec backend python -m scripts.check_auth --base-url http://127.0.0.1:8000   # 로그인~로그아웃 점검 (이메일·비밀번호 입력)
```

- 내 PC 브라우저로 로그인해 본 뒤 `$DC logs --tail 20 backend`의 접속 로그 IP가 **내 공인 IP**면 IP 기준 제한이 동작하는 것이다
  (`10.231.0.10`이 보이면 nginx·uvicorn 설정이 안 맞은 것).
- 로그인 뒤에는 사용자마다 자기 데이터만 보인다 (8번 "모두 같은 시연 사용자"는 로그인 전 이야기).
- HTTP라 로그인 비밀번호·토큰도 암호화되지 않고 오간다. 시연용 비밀번호만 쓰고, 오래 공개하면 HTTPS를 붙인다.

### 9-4. 예상 시간·비용 (약 2,100편 기준, 2026-10-27 측정으로 어림)

PubMed 기준 2024년 1,191편 + 2025년 1,257편(동물 전용 연구 제외 후) 중 2025년 363편은 이미 DB에 있어 **약 2,100편**이 새로 들어온다.

| 단계 | 편당 | 2,100편 |
|---|---|---|
| 수집 (24개월) | — | 5~10분 |
| 임베딩 (bge-m3, CPU) | 개발 PC(12코어) 0.9초. t3.medium(2 vCPU)은 3~5배 느릴 것으로 봄 → 약 3~5초 | **약 2~3시간** (스왑을 쓰면 더) |
| 근거 등급 (규칙 기반) | 아주 짧음 | 1분 안팎 |
| 요약 (OpenAI, 한 편씩) | 약 12초 (자동 검사에 걸려 다시 만들면 더) | **약 7시간** |

→ **모두 합쳐 약 9~11시간.** 저녁에 걸어 두고 다음 날 확인하는 정도로 잡는다.

요약 토큰은 편당 입력 약 3,700 + 출력 약 900 (다시 만든 경우 포함) → 2,100편이면 **입력 약 790만 + 출력 약 190만 토큰**.
비용 = 790만 × 입력 단가 + 190만 × 출력 단가. `gpt-5.6-luna`를 100만 토큰당 입력 $0.20 / 출력 $1.20으로 잡으면 **약 $4**,
$1.00 / $6.00이면 **약 $19**다 (단가는 OpenAI 가격표에서 확인한다).

## 11. HTTPS 전환 (mindcare-project.duckdns.org)

도메인 `mindcare-project.duckdns.org` + Let's Encrypt(webroot, 자동 갱신). 기숙사 망이 22를 막아 SSH도 443으로 들어오므로
**443 하나를 서버 본체 nginx(stream)가 받아 SSH와 HTTPS로 나눈다.**

```
443 → 호스트 nginx(stream, ssl_preread) ─┬ TLS가 아님 → 127.0.0.1:2222 (PROXY 머리말 떼기) → sshd 22
     (proxy_protocol로 실제 IP 전달)     └ TLS       → 127.0.0.1:8443 → web 컨테이너(HTTPS) → backend
80  → web 컨테이너 ─ 1단계: 지금 HTTP 서비스 그대로 + /.well-known/acme-challenge/
                    2단계: /.well-known/acme-challenge/ + 나머지는 https://mindcare-project.duckdns.org 로 301
```

- 호스트 nginx는 **80을 쓰지 않는다** (web 컨테이너가 씀). 기본 사이트는 11-1에서 끈다.
- 사용자 IP: 호스트 nginx가 PROXY protocol로 넘기고, web nginx는 **도커 게이트웨이 `10.231.0.1`에서 온 연결만** 그 값을 믿는다
  (`nginx.https.conf`의 `set_real_ip_from`). backend uvicorn은 지금처럼 web(`10.231.0.10`)이 보낸 `X-Forwarded-For`만 믿는다.
- **1단계 ↔ 2단계 전환은 `backend/.env`의 한 줄**이다. 바꾼 뒤 `$DC up -d web` (`restart`로는 안 바뀐다 — 붙이는 파일이 달라져 컨테이너를 다시 만들어야 함).

  | 단계 | `backend/.env` | web이 쓰는 설정 |
  |---|---|---|
  | 1단계 (인증서 없어도 뜸) | `WEB_NGINX_CONF` 줄 없음 | `deploy/nginx.conf` |
  | 2단계 (인증서 받은 뒤) | `WEB_NGINX_CONF=nginx.https.conf` | `deploy/nginx.https.conf` |

**SSH 위험 표시** — 🟢 SSH와 무관 / 🔴 **443 SSH가 끊길 수 있음 → AWS 콘솔의 EC2 Instance Connect(22번)에서 한다.**
🔴 단계 전에 Instance Connect로 한 번 들어가 보고(보안 그룹 22번이 Instance Connect 대역에 열려 있어야 한다),
끝난 뒤 **내 PC에서 `ssh -p 443`이 되는 것을 확인할 때까지 Instance Connect 창을 닫지 않는다.**

미리 확인: DuckDNS가 탄력적 IP를 가리키는지 (`getent hosts mindcare-project.duckdns.org`), 보안 그룹에 80·443이 열려 있는지.

### 11-1. 설치 🟢

```bash
# apt가 nginx를 설치하자마자 기본 사이트(80)로 띄우려다 web 컨테이너와 부딪쳐 설치가 실패하지 않게, 설치하는 동안만 서비스 시작을 막는다
printf '#!/bin/sh\nexit 101\n' | sudo tee /usr/sbin/policy-rc.d >/dev/null && sudo chmod +x /usr/sbin/policy-rc.d
sudo apt-get update
sudo apt-get install -y nginx libnginx-mod-stream
sudo rm /usr/sbin/policy-rc.d
sudo rm /etc/nginx/sites-enabled/default    # 기본 사이트(80) 끄기. 원본 sites-available/default는 남는다
sudo systemctl disable nginx                # 11-4에서 켠다 (그 전에 재부팅돼도 443을 잡지 않게)
systemctl is-active nginx                   # inactive
sudo apt-get install -y certbot             # 갱신 타이머(certbot.timer, 하루 두 번 확인)가 같이 켜진다
sudo mkdir -p /var/www/certbot
```

### 11-2. 1단계 배포 🟢

```bash
cd ~/mind-care && git pull
$DC up -d web     # nginx-app.inc·인증서 폴더 마운트·127.0.0.1:8443이 붙어 web만 다시 만든다 (아직 HTTP 서비스 그대로)
sudo mkdir -p /var/www/certbot/.well-known/acme-challenge
echo ok | sudo tee /var/www/certbot/.well-known/acme-challenge/ping >/dev/null
curl -s http://mindcare-project.duckdns.org/.well-known/acme-challenge/ping                # ok (기본 인증 없이)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/                                 # 401 (화면은 그대로)
sudo rm -r /var/www/certbot/.well-known
```

### 11-3. 인증서 발급 🟢

```bash
sudo certbot certonly --webroot -w /var/www/certbot -d mindcare-project.duckdns.org \
    --email 이메일주소 --agree-tos --no-eff-email      # 이메일: 만료 알림을 받을 주소로 바꿔 넣는다
sudo ls /etc/letsencrypt/live/mindcare-project.duckdns.org/   # fullchain.pem privkey.pem ...

# 갱신되면 web nginx가 새 인증서를 읽게 (certbot이 갱신 때마다 이 폴더의 스크립트를 돌린다)
sudo install -m 755 deploy/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/mindcare-web-reload.sh
sudo certbot renew --dry-run                  # 갱신 연습 (webroot 방식이 저장돼 있다. 훅은 연습 때 안 돈다)
systemctl list-timers certbot.timer           # 다음 확인 시각
```

### 11-4. sshd 443 끄기 → 호스트 nginx 켜기

**① 준비 🟢** (포트를 잡지 않으므로 지금 SSH 그대로 해도 된다)

```bash
cd ~/mind-care
sudo install -m 644 deploy/host-nginx/mindcare-stream.conf /etc/nginx/mindcare-stream.conf
# stream은 http 블록 밖이어야 하므로 nginx.conf 맨 끝에 한 줄
grep -q mindcare-stream /etc/nginx/nginx.conf || echo 'include /etc/nginx/mindcare-stream.conf;' | sudo tee -a /etc/nginx/nginx.conf
sudo nginx -t                                                       # syntax is ok

# sshd가 443을 어디서 받는지 확인
sudo sshd -T | grep -i '^port'                                      # port 22, port 443
sudo grep -rn -i '^\s*port\s' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/
systemctl is-active ssh.socket                                      # active면 소켓 방식 (우분투 24.04 기본)
systemctl cat ssh.socket | grep -i listen                           # ListenStream에 443을 직접 넣었다면 여기 보인다
```

**② 전환 🔴 — EC2 Instance Connect에서**

```bash
# 1) Port 443 줄을 지운다. Port 22 줄은 남긴다 (Port 줄이 443 하나뿐이었다면 Port 22로 바꾼다).
#    ①에서 ssh.socket 쪽 ListenStream=443이 보였다면 그 override 파일에서 그 줄을 지운다.
sudo nano /etc/ssh/sshd_config           # (①의 grep이 가리킨 파일)
sudo sshd -t && sudo sshd -T | grep -i '^port'           # port 22만

# 2) 적용 — ssh.socket이 active였으면 첫 줄, 아니면 둘째 줄
sudo systemctl daemon-reload && sudo systemctl restart ssh.socket
# sudo systemctl restart ssh

# 3) 443이 비었는지 보고 호스트 nginx 켜기
sudo ss -ltnp | grep ':443 '             # 아무것도 안 나와야 한다
sudo systemctl enable --now nginx
sudo ss -ltnp | grep -E ':(443|2222) '   # nginx가 0.0.0.0:443, 127.0.0.1:2222
```

내 PC에서 `ssh -p 443 -i C:\Users\PJH\.ssh\mindcare-ec2.pem ubuntu@mindcare-project.duckdns.org`가 되면 Instance Connect를 닫는다.
안 되면 Instance Connect 창에서 바로 11-7 ②로 되돌린다.

이 시점엔 web이 아직 1단계라 화면은 `http://`로 그대로 열리고, `https://`만 아직 안 된다.

### 11-5. 2단계 배포 🟢

```bash
cd ~/mind-care
echo 'WEB_NGINX_CONF=nginx.https.conf' >> backend/.env
$DC up -d web
$DC logs --tail 20 web                   # [emerg]가 없으면 OK
```

### 11-6. 확인 🟢

내 PC(PowerShell):

```powershell
curl.exe -sI http://mindcare-project.duckdns.org/feed          # 301, Location: https://mindcare-project.duckdns.org/feed
curl.exe -sI http://탄력적IP/                                   # 301, 도메인으로
curl.exe -s -o NUL -w "%{http_code} %{http_version}\n" https://mindcare-project.duckdns.org/      # 401 2 (기본 인증, HTTP/2)
curl.exe -s -o NUL -w "%{http_code}\n" https://mindcare-project.duckdns.org/api/health            # 200
curl.exe -s -o NUL -w "%{http_code}\n" https://mindcare-project.duckdns.org/api/docs              # 404
```

서버:

```bash
sudo tail -n 5 /var/log/nginx/mindcare-stream.log   # 내 공인 IP와 127.0.0.1:8443(HTTPS)·127.0.0.1:2222(SSH)
docker network inspect mindcare_default -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'   # 10.231.0.1 (nginx.https.conf와 같아야)
$DC logs --tail 5 web                               # 맨 앞 IP가 내 공인 IP (10.231.0.1이면 위 게이트웨이가 다른 것)
```

브라우저에서 `https://mindcare-project.duckdns.org/` → 기본 인증 → 로그인 → `$DC logs --tail 20 backend`의 접속 IP가 내 공인 IP인지 (10번과 같음).

### 11-7. 되돌리기

**① HTTPS만 되돌리기 🟢** (web이 다시 80에서 화면을 낸다. SSH·호스트 nginx는 그대로)

```bash
cd ~/mind-care
sed -i '/^WEB_NGINX_CONF=/d' backend/.env
$DC up -d web
```

**② 443을 다시 sshd로 🔴 — EC2 Instance Connect에서** (①도 같이 한다)

```bash
sudo systemctl disable --now nginx
sudo nano /etc/ssh/sshd_config           # Port 443 줄을 다시 넣는다 (Port 22는 그대로)
sudo sshd -t
sudo systemctl daemon-reload && sudo systemctl restart ssh.socket    # 소켓 방식이 아니면: sudo systemctl restart ssh
sudo ss -ltnp | grep ':443 '             # sshd
```

### 주의

- **SSH 클라이언트가 먼저 말해야 나뉜다.** OpenSSH(`ssh`·`scp`·`sftp`, VS Code Remote-SSH)는 접속하자마자 자기 버전을 보내서 바로 SSH로 간다
  (로컬 도커로 확인). 서버 인사말을 먼저 기다리는 도구(`ssh-keyscan` 등)는 443으로는 30초 뒤 끊긴다 → 그런 건 Instance Connect에서.
- sshd 로그(`/var/log/auth.log`)에는 443으로 들어온 SSH가 **모두 127.0.0.1**로 찍힌다. 실제 IP는 `/var/log/nginx/mindcare-stream.log`.
  sshd 로그로 IP를 막는 도구(fail2ban 등)를 쓰면 모두를 막게 되니 쓰지 않는다.
- 8443은 PROXY protocol이 있어야 받으므로 서버 안에서 `curl https://127.0.0.1:8443`으로는 확인할 수 없다. 도메인으로 확인한다.
- 2단계부터 `http://127.0.0.1/…`는 301이다. 8-3·10-4의 확인 명령은 `https://mindcare-project.duckdns.org/…`로 바꿔 친다.
- 인증서 갱신은 80번의 `/.well-known/acme-challenge/`로 확인받는다. **보안 그룹에서 80을 닫으면 갱신이 실패한다** (90일 인증서, 만료 30일 전부터 갱신 시도).
- 호스트 nginx 설정(`deploy/host-nginx/`)을 고쳤으면 다시 복사 → `sudo nginx -t && sudo systemctl reload nginx` (reload는 연결된 SSH를 끊지 않는다).
- HSTS 헤더는 아직 넣지 않았다 (한 번 걸면 되돌리기 어렵다). HTTPS가 안정되면 `nginx.https.conf`에 추가를 검토한다.
