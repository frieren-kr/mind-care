# 서버 배포 (AWS EC2)

> 담당: **박주현**. 공통 규칙은 루트 [`AGENTS.md`](../AGENTS.md), 배치 동작은 [`db.md`](db.md)의 "통합 배치".

EC2 한 대(Ubuntu 24.04, t3.medium 메모리 4GB + 스왑 4GB, Docker 설치됨)에 **DB + 백엔드 + 주기 배치 + 웹(nginx)**을 올린다.

| 파일 | 하는 일 |
|---|---|
| `backend/Dockerfile` | 백엔드 이미지 (python 3.12-slim, torch **CPU 전용** 2.14.1 — 로컬 `.venv`와 같은 버전) |
| `backend/.dockerignore` | `.env`·덤프·로그·캐시가 이미지에 들어가지 않게 |
| `deploy/docker-compose.prod.yml` | 서버용 compose. db(127.0.0.1:5432) + backend(127.0.0.1:8000) + web(80) |
| `deploy/web.Dockerfile` (+ `.dockerignore`) | 프론트 웹 빌드(`expo export`) → nginx 이미지 |
| `deploy/nginx.conf` | `/` 정적 파일, `/api/` → backend, 사이트 전체 기본 인증 |
| `deploy/run_update_papers.sh` | 실행 중인 backend 컨테이너 안에서 `update_papers` 배치를 돌린다 (cron용) |

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
- `OPENAI_SUMMARY_API_KEY`(없으면 `OPENAI_API_KEY`), `OPENAI_SUMMARY_MODEL` — 요약
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
$DC exec backend ls -l logs                                   # 배치 로그 목록 (update_papers_YYYY-MM-DD.log)
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
**사이트 전체에 HTTP 기본 인증**(팀 공용 아이디·비밀번호 하나)이 걸린다. db·backend 포트는 그대로 `127.0.0.1`만.

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
- **`deploy/nginx.conf`만 바뀌었을 때**: 볼륨으로 붙어 있어 빌드 없이 `$DC restart web`.
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
