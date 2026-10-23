"""새 논문 수집 → 임베딩 → 근거 등급 → 요약을 한 번에 돌린다. (담당: 박주현)

cron으로 주기 실행할 배치의 진입점이다. 단계마다 "아직 처리 안 된 논문"만 골라서 처리하므로
여러 번 돌려도 결과가 같고, 중간에 끊기거나 한 단계가 실패해도 다음 실행 때 남은 것부터 이어서 한다.

  ① 수집      collect_and_store()                — 최근 N일 PubMed. 이미 있는 논문은 save_papers()가 건너뛴다
  ② 임베딩    get_papers_without_embedding()      — bge-m3 임베딩이 없는 논문 → embed_documents() (현서 님)
  ③ 근거 등급  get_papers_without_evidence()       — study_type이 빈 논문 → classify() (현서 님)
  ④ 요약      get_new_papers(--limit)             — 요약이 없는 논문 → summarize_papers.run()

embed_papers.py / classify_papers.py는 main()(argparse + print)만 있어서 부를 수 없다.
그래서 두 스크립트가 쓰는 AI 함수(embed_documents / classify)와 DB 함수를 같은 순서로 직접 부른다.

종료 코드: 0 전부 성공(이미 실행 중이라 건너뛴 경우 포함) / 1 일부 실패 / 2 실행 불가(DB 연결 실패, 키 없음)

실행 (backend/ 에서):
    python -m scripts.update_papers --log-file logs/update_papers.log
    python -m scripts.update_papers --skip-collect --limit 20
    python -m scripts.update_papers --only summarize --dry-run
    python -m scripts.update_papers --only summarize --retry-skipped

요약에 MAX_FAILS번 실패한 논문은 상태 파일(--state-file)에 적어 두고 다음 실행부터 건너뛴다.
건너뛴 논문은 종료 코드 1의 원인이 아니다. --retry-skipped로 다시 시도한다.
"""
import argparse
import json
import logging
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows(개발 PC)에는 fcntl이 없다. 서버(Linux)는 fcntl.flock을 쓴다.
    fcntl = None
    import msvcrt

from app.config import settings
from app.db import functions

logger = logging.getLogger("update_papers")

EMBED_CHUNK = 16     # embed_papers.CHUNK와 같다 (자주 저장 → 끊겨도 손해 적음)
CLASSIFY_CHUNK = 200  # classify_papers.CHUNK와 같다
DEFAULT_LOCK = Path(tempfile.gettempdir()) / "mindcare_update_papers.lock"
STATE_NAME = "update_papers_state.json"
MAX_FAILS = 3  # 요약이 이만큼 실패한 논문은 다음 실행부터 건너뛴다


def load_state(path: Path) -> dict:
    """{논문 id: {"fails": n, "pmid": "..."}}. 없거나 깨졌으면 빈 기록으로 시작한다."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return {k: v for k, v in state.items() if isinstance(v, dict)}
        logger.warning("  상태 파일 형식이 달라 새로 시작합니다: %s", path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("  상태 파일을 읽지 못해 새로 시작합니다 (%s): %s", type(exc).__name__, path)
    return {}


def save_state(path: Path, state: dict) -> None:
    """임시 파일에 쓰고 바꿔치기한다 (쓰다 끊겨도 기존 파일이 깨지지 않게). 실패해도 실행은 계속한다."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("  상태 파일 저장 실패 (%s): %s", type(exc).__name__, path)


def stage_collect(args) -> tuple[int, int, str]:
    from app.collectors import pubmed

    if args.dry_run:
        papers = pubmed.fetch_papers(days_back=args.days_back, max_results=args.max_results)
        return len(papers), 0, f"최근 {args.days_back}일 {len(papers)}편 받음 (저장 안 함)"
    result = pubmed.collect_and_store(days_back=args.days_back, max_results=args.max_results)
    return result.inserted, 0, f"최근 {args.days_back}일 {result.total}편 중 신규 {result.inserted}, 중복 {result.skipped}"


def stage_embed(args) -> tuple[int, int, str]:
    from app.ai.retrieval.embedding import MODEL_NAME, embed_documents, free_memory, paper_text

    if args.dry_run:
        n = len(functions.get_papers_without_embedding(MODEL_NAME, limit=100_000))
        return 0, 0, f"대상 {n}편 (dry-run: 임베딩하지 않음)"
    done = failed = 0
    while papers := functions.get_papers_without_embedding(MODEL_NAME, limit=EMBED_CHUNK):
        vectors = embed_documents([paper_text(p.title, p.abstract) for p in papers])
        result = functions.save_embeddings(list(zip([p.id for p in papers], vectors)), MODEL_NAME)
        free_memory()
        done, failed = done + result.saved, failed + len(result.not_found)
        if result.saved == 0:  # 저장이 안 되면 같은 논문이 계속 나오므로 멈춘다
            raise RuntimeError(f"임베딩 저장 0건이라 중단 (not_found {len(result.not_found)})")
    return done, failed, f"모델 {MODEL_NAME}"


def stage_classify(args) -> tuple[int, int, str]:
    from app.ai.retrieval.evidence import classify

    def items(papers):
        return [
            (p.paper_id, *classify(p.publication_types, p.mesh_terms, p.title, p.abstract)[:2])
            for p in papers
        ]

    if args.dry_run:  # 저장하지 않으면 같은 논문이 계속 나오므로 한 번에 받는다
        n = len(items(functions.get_papers_without_evidence(limit=100_000)))
        return n, 0, "판단만 하고 저장 안 함"
    done = failed = 0
    while papers := functions.get_papers_without_evidence(limit=CLASSIFY_CHUNK):
        result = functions.save_evidence(items(papers))
        done, failed = done + result.saved, failed + len(result.not_found)
        if result.saved == 0:
            raise RuntimeError(f"등급 저장 0건이라 중단 (not_found {len(result.not_found)})")
    return done, failed, ""


def stage_summarize(args) -> tuple[int, int, str]:
    from openai import OpenAI

    from scripts.summarize_papers import run, summary_api_key

    key_name, api_key = summary_api_key()
    model = settings.openai_summary_model
    logger.info("  OpenAI 키: %s, 모델 %s", key_name, model)  # 키 값은 출력하지 않는다

    state = load_state(args.state_file)
    blocked = set() if args.retry_skipped else {k for k, v in state.items() if v.get("fails", 0) >= MAX_FAILS}
    # 건너뛸 논문이 limit 자리를 차지하지 않게 그만큼 더 받아서 거른다
    candidates = functions.get_new_papers(limit=args.limit + len(blocked))
    skipped = [p for p in candidates if str(p.id) in blocked]
    targets = [p for p in candidates if str(p.id) not in blocked][:args.limit]
    if skipped:
        logger.info("  건너뜀 %d건 (%s) — 요약 %d번 실패. 다시 하려면 --retry-skipped",
                    len(skipped), ", ".join(p.external_id for p in skipped), MAX_FAILS)

    papers = [(p.id, p.title, p.abstract) for p in targets]
    # 재시도는 run() 안의 ask()가 센다 (SDK 자체 재시도와 겹치지 않게 0).
    report = run(papers, OpenAI(api_key=api_key, max_retries=0), model, dry_run=args.dry_run)
    pmids = {p.id: p.external_id for p in targets}
    for number, paper_id, reason in report.failed:
        logger.warning("  요약 실패 %s (PMID %s): %s", paper_id, pmids.get(paper_id), reason)
    if not args.dry_run:  # dry-run은 아무것도 바꾸지 않는다
        for _, paper_id, *_ in report.done:
            state.pop(str(paper_id), None)
        for _, paper_id, _ in report.failed:
            entry = state.setdefault(str(paper_id), {"fails": 0, "pmid": pmids.get(paper_id)})
            entry["fails"] = entry.get("fails", 0) + 1
        save_state(args.state_file, state)
    return len(report.done), len(report.failed), (
        f"대상 {len(papers)}편, 건너뜀 {len(skipped)}, 초록 없음 {report.no_abstract}, "
        f"토큰 {report.prompt_tokens + report.completion_tokens:,}"
        + (" (dry-run: 저장 안 함)" if args.dry_run else "")
    )


STAGES = {"collect": stage_collect, "embed": stage_embed, "classify": stage_classify, "summarize": stage_summarize}
STAGE_NAMES = {"collect": "수집", "embed": "임베딩", "classify": "근거 등급", "summarize": "요약"}


def check_ready(stages: list[str]) -> str | None:
    """실행할 수 없는 이유를 돌려준다 (없으면 None). 키는 변수 이름만 말한다."""
    if "summarize" in stages:
        from scripts.summarize_papers import summary_api_key

        if not summary_api_key()[1]:
            return "OpenAI 키가 없습니다 (OPENAI_SUMMARY_API_KEY 또는 OPENAI_API_KEY)"
        if not settings.openai_summary_model:
            return "OPENAI_SUMMARY_MODEL이 비어 있습니다"
    try:
        functions.get_new_papers(limit=1)  # DB 연결 확인 (읽기만)
    except Exception as exc:
        return f"DB 연결 실패: {type(exc).__name__}: {exc}".strip()
    return None


def acquire_lock(path: Path):
    """잠금 파일을 잡는다. 이미 다른 실행이 잡고 있으면 None. 돌려받은 파일을 닫으면 풀린다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True
    )
    # PubMed 요청 URL에 api_key가 들어가므로 httpx의 요청 로그(INFO)는 남기지 않는다.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="논문 수집 → 임베딩 → 근거 등급 → 요약")
    ap.add_argument("--skip-collect", action="store_true", help="수집 단계를 건너뛴다")
    ap.add_argument("--only", choices=list(STAGES), help="이 단계만 실행한다")
    ap.add_argument("--limit", type=int, default=100, help="요약 최대 편수 (기본 100)")
    ap.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않는다 (요약은 LLM을 부르지만 저장하지 않음)")
    ap.add_argument("--days-back", type=int, default=7, help="수집: 최근 N일 발행분 (기본 7, cron 주기보다 길게)")
    ap.add_argument("--max-results", type=int, default=100, help="수집: 최대 편수 (기본 100)")
    ap.add_argument("--log-file", type=Path, help="로그를 화면과 함께 남길 파일")
    ap.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK, help=f"겹쳐 실행 방지용 잠금 파일 (기본 {DEFAULT_LOCK})")
    ap.add_argument("--state-file", type=Path,
                    help=f"요약 실패 횟수 기록 (기본: --log-file과 같은 폴더, 없으면 임시 폴더의 {STATE_NAME})")
    ap.add_argument("--retry-skipped", action="store_true", help=f"요약 {MAX_FAILS}번 실패해 건너뛰던 논문도 다시 시도한다")
    args = ap.parse_args(argv)
    if args.state_file is None:
        args.state_file = (args.log_file.parent if args.log_file else DEFAULT_LOCK.parent) / STATE_NAME
    setup_logging(args.log_file)

    lock = acquire_lock(args.lock_file)
    if lock is None:
        logger.warning("이미 실행 중입니다 (잠금 파일 %s). 이번 실행은 건너뜁니다.", args.lock_file)
        return 0
    try:
        return run_stages(args)
    finally:
        lock.close()


def run_stages(args) -> int:
    stages = [args.only] if args.only else [s for s in STAGES if not (args.skip_collect and s == "collect")]
    started = time.monotonic()
    logger.info("시작 %s — 단계 %s%s", datetime.now().astimezone().isoformat(timespec="seconds"),
                " → ".join(STAGE_NAMES[s] for s in stages), " (dry-run)" if args.dry_run else "")
    if problem := check_ready(stages):
        logger.error("실행할 수 없습니다: %s", problem)
        return 2

    exit_code = 0
    for name in stages:
        t0 = time.monotonic()
        try:
            done, failed, note = STAGES[name](args)
        except Exception as exc:  # 한 단계가 통째로 실패해도 앞 단계 결과는 이미 저장됐고 다음 단계는 계속한다
            logger.exception("[%s] 실패 (%.1f초): %s", STAGE_NAMES[name], time.monotonic() - t0, type(exc).__name__)
            exit_code = 1
            continue
        logger.info("[%s] 처리 %d건, 실패 %d건, %.1f초%s", STAGE_NAMES[name], done, failed,
                    time.monotonic() - t0, f" — {note}" if note else "")
        if failed:
            exit_code = 1

    logger.info("끝 %s — 총 %.1f초, 종료 코드 %d", datetime.now().astimezone().isoformat(timespec="seconds"),
                time.monotonic() - started, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
