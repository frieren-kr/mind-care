# 마인드 케어 (Mind Care)

치매·경도인지장애 가족 간병인을 위한 연구 요약·개인화 앱. PubMed 논문을 주기적으로 수집해 LLM + RAG로 관련성·근거를 분류·요약하고, 간병인에게 쉬운 말로 전달하며 개인화 챗봇으로 답합니다. (팀 온기억, 산학프로젝트2 Fall 2026)

## 기술 스택

- **백엔드**: Python + FastAPI
- **프론트엔드**: React Native + Expo
- **DB**: PostgreSQL + pgvector
- **AI**: 범용 LLM API(GPT/Gemini) + RAG + Embedding
- **논문 수집**: PubMed(NCBI E-utilities) API — 주 1회 배치

## 빠른 시작

### 1. DB 띄우기

DB는 Docker로 띄웁니다 (준비물: Docker Desktop). 최초 기동 시 `app/db/migrations/` 안의 SQL이
파일명 순서대로(`001` → `002` → …) 자동 적용됩니다.

```bash
cd backend
cp .env.example .env          # POSTGRES_PASSWORD / DATABASE_URL 채우기 (.env는 커밋 금지)
docker compose up -d          # PostgreSQL 17 + pgvector 기동
docker compose ps             # STATUS가 "healthy"면 준비 완료
```

### 2. 이미 DB를 띄워 둔 사람: 새 마이그레이션 적용

자동 적용은 **DB를 처음 만들 때(볼륨이 비어 있을 때) 한 번만** 일어납니다. 그 뒤에 추가된 마이그레이션은
`backend/` 에서 아래 명령으로 **002부터 최신 번호까지 순서대로 전부** 적용하세요.
002~010은 모두 **여러 번 실행해도 안전**하므로, 이미 적용한 파일이 섞여 있어도 됩니다
(빈 DB에 두 번씩 적용해 확인, 2026-10-22). 파일마다 `--single-transaction`이라 실패한 파일은 통째로
되돌아가고, 거기서 멈춥니다.

```powershell
# Windows PowerShell (한글이 깨지지 않게 파일을 컨테이너에 복사해서 실행)
docker cp app\db\migrations mindcare-db:/tmp/migrations
foreach ($f in (Get-ChildItem app\db\migrations\0*.sql | Sort-Object Name)) {
    if ($f.Name -eq '001_init.sql') { continue }
    Write-Host $f.Name
    docker exec mindcare-db psql -U mindcare -d mindcare -q -v ON_ERROR_STOP=1 --single-transaction -f "/tmp/migrations/$($f.Name)"
    if ($LASTEXITCODE -ne 0) { Write-Host "$($f.Name) 적용 실패 - 여기서 멈춤"; break }
}
docker exec mindcare-db rm -rf /tmp/migrations
```

```bash
# macOS / Linux / Git Bash
for f in app/db/migrations/0*.sql; do
  [ "$(basename "$f")" = 001_init.sql ] && continue
  echo "$f"
  docker exec -i mindcare-db psql -U mindcare -d mindcare -q -v ON_ERROR_STOP=1 --single-transaction < "$f" \
    || { echo "$f 적용 실패 - 여기서 멈춤"; break; }
done
```

이미 적용한 파일은 `NOTICE: ... already exists, skipping`을 남기고 지나갑니다 (정상).

**처음 적용할 때** 데이터 때문에 지워지거나 멈추는 경우 (다시 실행할 때는 해당 없음):

- `003` — 768차원 임베딩이 남아 있으면 **지웁니다** (1024차원으로 바꿀 수 없음). 아래 3번 덤프를 다시 받으면 됩니다.
- `006` — 같은 기간 월간 요약(`profile_summaries`)이 중복으로 쌓여 있으면 멈춥니다. 중복을 정리하고 다시 실행하세요.
- `008` — 008 전에 `guideline_knowledge`에 행이 있으면 멈춥니다 (`TRUNCATE guideline_knowledge;` 후 다시 실행).
- `010` — 날짜가 미래인데 "방문 완료"인 진료 기록, 종료일이 미래인데 "복용 중단"인 복약 기록이 있으면 멈춥니다. 고친 뒤 다시 실행하세요.

파일별 내용은 [`docs/db.md`](docs/db.md)의 마이그레이션 목록에 있습니다.
(DB를 처음부터 다시 만들어도 됩니다 — `docker compose down -v` 후 `up -d`. 단, **저장된 데이터가 모두 지워집니다.**)

### 3. 팀 기준 논문 데이터 받기 (덤프)

팀은 같은 논문 데이터를 공유합니다. **`papers`(논문), `paper_embeddings`(bge-m3 임베딩),
`paper_analysis`(요약·한국어 제목·근거 등급) 3개 테이블을 한 파일**(`mindcare_papers_YYYYMMDD.dump`,
데이터만)로 받아 복원합니다. 덤프는 용량이 커서 레포에 넣지 않고 **박주현이 팀 단톡에 올립니다.**

> 같은 덤프로 복원하면 모든 팀원의 `papers.id`(= AI가 쓰는 `paper_id`)가 똑같아져서,
> 임베딩·요약·검색 결과를 그대로 주고받을 수 있습니다.

1. 단톡에서 덤프를 받아 레포 밖(예: 바탕화면)에 둡니다.
2. 위 2번까지 해서 **스키마를 최신으로** 맞춥니다 (덤프에는 스키마가 없습니다).
3. 기존 논문을 비우고 복원합니다. PowerShell은 `<` 입력이 안 되므로 `docker cp`로 넣고 컨테이너 안에서 복원합니다.

```powershell
# Windows PowerShell — 덤프 경로·날짜는 받은 파일로 바꾸세요 (macOS / Linux도 경로만 바꾸면 같습니다)
docker exec mindcare-db psql -U mindcare -d mindcare -c "TRUNCATE papers CASCADE;"
docker cp "$HOME\Desktop\mindcare_papers_YYYYMMDD.dump" mindcare-db:/tmp/restore.dump
docker exec mindcare-db pg_restore -U mindcare -d mindcare --data-only --single-transaction /tmp/restore.dump
docker exec mindcare-db rm /tmp/restore.dump
```

- `TRUNCATE papers CASCADE`는 논문을 가리키는 `paper_embeddings`·`paper_analysis`·`feed_items`도 같이 비웁니다.
- `--single-transaction`이라 복원이 중간에 실패하면 복원분은 전부 되돌아갑니다.
- Git Bash에서는 `/tmp/...` 경로가 Windows 경로로 바뀌므로 명령 앞에 `MSYS_NO_PATHCONV=1`을 붙이세요.

확인:

```powershell
docker exec mindcare-db psql -U mindcare -d mindcare -c "SELECT (SELECT count(*) FROM papers) papers, (SELECT count(*) FROM paper_embeddings) emb, (SELECT count(summary_finding) FROM paper_analysis) summarized;"
```

덤프 파일은 `.env`처럼 **레포에 커밋하지 않습니다.**

### 4. 개발용 샘플 사용자 넣기

피드·추천·챗봇 개발용 예시 간병인 4명(환자 5명)입니다. 실제 사람 정보가 아닙니다.
가상환경을 켜고 `backend/` 에서 실행하세요.

```bash
python -m scripts.seed_dev_profiles   # 이미 있으면 건너뜀 — 여러 번 실행해도 안전 (다시 넣으려면 --reset)
python -m scripts.list_users          # user_id / patient_id 확인
```

`user_id`는 넣을 때마다 새로 만들어져서 **팀원마다 다릅니다.** 이 값으로 `/feed/{user_id}` 같은 주소를 호출해 볼 수 있습니다.

### 5. 백엔드·프론트엔드 실행

```bash
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1    # Windows PowerShell
pip install -r requirements.txt
uvicorn main:app --reload     # http://127.0.0.1:8000
```

DB가 떠 있는 상태로 실행합니다. 브라우저에서 **Swagger 문서 http://127.0.0.1:8000/docs** 를 열면
API 주소와 응답 예시를 보고 바로 호출해 볼 수 있습니다 (주소 목록은 [`docs/api.md`](docs/api.md)).

```bash
cd frontend
npm install
npm start
```

DB 확인·문제 해결은 [`backend/README.md`](backend/README.md), 서버 배포·주기 배치는 [`docs/deploy.md`](docs/deploy.md)를 참고하세요.

## 팀 협업 규칙

규칙과 담당 표는 [`AGENTS.md`](AGENTS.md)에 있습니다 (`develop`에 push, `main`은 PR + 팀장 승인, `.env`·키 커밋 금지 등).

> **사용하는 AI 도구가 `AGENTS.md`를 읽는지 확인해주세요.**
> Gemini CLI는 설정에서 `AGENTS.md`를 지정해야 합니다.

## 폴더 구조

```
mind-care/
├─ backend/     FastAPI 백엔드
│  ├─ main.py           앱 진입점
│  ├─ app/
│  │  ├─ config.py      환경 변수
│  │  ├─ schemas.py     데이터 접근 함수 입출력 스키마
│  │  ├─ api/           API 라우터 (기능별 파일: papers.py …)
│  │  ├─ db/            DB 접근 함수 계층 (팀 공용 관문) + migrations/
│  │  └─ collectors/    PubMed 수집
│  └─ requirements.txt
├─ frontend/    React Native(Expo) 앱
├─ deploy/      서버용 compose·배치 스크립트
├─ docs/        파트별 상세 문서 (db·api·users·ai·deploy·history)
└─ AGENTS.md    팀 협업 규칙·담당 표 (모든 AI 도구 공통)
```
