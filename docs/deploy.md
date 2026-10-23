# 서버 배포 (AWS EC2)

> 담당: **박주현**. 공통 규칙은 루트 [`AGENTS.md`](../AGENTS.md), 배치 동작은 [`db.md`](db.md)의 "통합 배치".

EC2 한 대(Ubuntu 24.04, t3.medium 메모리 4GB + 스왑 4GB, Docker 설치됨)에 **DB + 백엔드 + 주기 배치**를 올린다.

| 파일 | 하는 일 |
|---|---|
| `backend/Dockerfile` | 백엔드 이미지 (python 3.12-slim, torch **CPU 전용** 2.14.1 — 로컬 `.venv`와 같은 버전) |
| `backend/.dockerignore` | `.env`·덤프·로그·캐시가 이미지에 들어가지 않게 |
| `deploy/docker-compose.prod.yml` | 서버용 compose. db(127.0.0.1:5432) + backend(127.0.0.1:8000) |
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

## 3. 실행

```bash
$DC up -d --build
$DC ps                      # db가 healthy, backend가 running이면 OK
$DC logs -f backend         # "임베딩 모델 예열 완료"가 보일 때까지 (첫 기동은 bge-m3 약 2GB를 받느라 몇 분)
curl http://127.0.0.1:8000/docs -o /dev/null -w "%{http_code}\n"   # 200
```

- **첫 기동(빈 `pgdata` 볼륨)일 때만** 마이그레이션 `001`~`008`이 파일명 순서대로 자동 적용된다.
- 포트는 서버 안(`127.0.0.1`)에서만 열려 있다. 외부 공개는 리버스 프록시(nginx 등)를 붙일 때 정한다.

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
