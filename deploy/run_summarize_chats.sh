#!/usr/bin/env bash
# 실행 중인 backend 컨테이너 안에서 챗봇 대화 하루 요약 + 7일 지난 원문 삭제(scripts/summarize_chats.py)를 돌린다. (담당: 박주현)
# 로그는 logs 볼륨에 날짜별(summarize_chats_YYYY-MM-DD.log)로 남고, 종료 코드는 배치의 것을 그대로 돌려준다
# (0 성공 / 1 일부 실패 / 2 실행 불가). 추가 인자는 배치에 그대로 넘긴다 (예: --date 2026-11-05, --dry-run).
set -u
cd "$(dirname "$0")/.."  # 레포 루트 (cron은 홈 폴더에서 실행하므로)

docker compose --env-file backend/.env -f deploy/docker-compose.prod.yml exec -T backend \
    python -m scripts.summarize_chats \
    --log-file "logs/summarize_chats_$(date +%F).log" \
    "$@"
