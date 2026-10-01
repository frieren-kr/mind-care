"""데이터 접근 함수 (박주현 담당).

★ 팀 규칙 ★
AI/백엔드 팀원은 DB에 직접 SQL을 쓰지 않는다.
반드시 이 모듈의 함수를 통해서만 데이터에 접근한다.
함수 이름 / 입출력(JSON) 형식이 바뀌면 반드시 AGENTS.md의 "데이터 접근 계약"에 기록한다.

테이블 정의는 `app/db/migrations/001_init.sql`이 정본이다.
search_similar()만 아직 시그니처와 반환 스키마를 확정한 스텁이다.
나머지 함수는 구현되어 있다.
"""
import json
import logging
from typing import Optional
from uuid import UUID

from pgvector.psycopg import Vector
from psycopg.rows import dict_row

from app.db.connection import get_connection
from app.schemas import (
    EMBEDDING_DIM,
    AnalysisIn,
    EmbeddingIn,
    PaperDetail,
    PaperIn,
    PaperOut,
    SaveEmbeddingsResult,
    SavePapersResult,
    SearchResult,
    UpdateMetadataResult,
)

logger = logging.getLogger(__name__)

# papers에 넣을 때 쓰는 컬럼 정의 — jsonb_to_recordset이 JSON을 이 타입으로 바꾼다.
# publication_types / mesh_terms는 TEXT[]라서 unnest(%s::text[]) 방식으로는
# 행마다 길이가 달라 표현할 수 없다. 그래서 배치 전체를 JSON 한 덩어리로 보낸다.
_PAPER_COLUMNS = """
    source text, external_id text, title text, abstract text,
    published_date date, url text, journal text, doi text,
    publication_types text[], mesh_terms text[]
"""


def _papers_as_json(papers: list[PaperIn]) -> str:
    """PaperIn 목록을 jsonb_to_recordset에 넘길 JSON 문자열로 바꾼다 (date → 'YYYY-MM-DD')."""
    return json.dumps([p.model_dump(mode="json") for p in papers])


def _as_vector_literal(embedding: list[float]) -> str:
    """파이썬 리스트를 pgvector 입력 리터럴 문자열로 바꾼다 ([1,2,3] 형태, 공백 없음).

    jsonb_to_recordset으로 배치를 한 번에 넣을 때 vector 타입을 바로 만들 수 없어서,
    JSON에는 이 문자열로 담아 보내고 SQL에서 ::vector로 되돌린다.
    """
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


def save_papers(papers: list[PaperIn]) -> SavePapersResult:
    """수집기가 가져온 논문을 papers 테이블에 저장한다.

    (source, external_id) UNIQUE 제약 + ON CONFLICT DO NOTHING 으로
    이미 있는 논문은 자동으로 건너뛴다. 기존 행은 갱신하지 않는다.
    ★ 이 "중복이면 건너뛰기" 동작은 다른 파트가 전제로 쓰고 있으므로 바꾸지 않는다.
      이미 있는 논문의 메타데이터를 채우려면 update_paper_metadata()를 쓴다.

    journal / doi / publication_types / mesh_terms(002 마이그레이션)도 함께 저장한다.

    Args:
        papers: 저장할 논문 목록 (schemas.PaperIn). 배치 안에 같은
            (source, external_id)가 여러 번 있으면 첫 건만 남긴다.

    Returns:
        SavePapersResult — 시도 건수 / 신규 저장 건수 / 중복 스킵 건수 +
        새로 저장된 papers.id 목록.
    """
    # 배치 내 중복 제거 (첫 건 우선) — 집계 숫자를 정확하게 맞추기 위함.
    unique: dict[tuple[str, str], PaperIn] = {}
    for paper in papers:
        unique.setdefault((paper.source, paper.external_id), paper)
    rows = list(unique.values())

    if not rows:
        return SavePapersResult(total=0, inserted=0, skipped=0, inserted_ids=[])

    # 배치 전체를 JSON 한 덩어리로 보내 한 번의 INSERT로 넣는다 (왕복 1회).
    # ON CONFLICT DO NOTHING — 이미 있는 논문은 건너뛰고 기존 행은 그대로 둔다.
    sql = f"""
        INSERT INTO papers (source, external_id, title, abstract, published_date, url,
                            journal, doi, publication_types, mesh_terms)
        SELECT source, external_id, title, abstract, published_date, url,
               journal, doi,
               coalesce(publication_types, '{{}}'), coalesce(mesh_terms, '{{}}')
          FROM jsonb_to_recordset(%s::jsonb) AS x({_PAPER_COLUMNS})
        ON CONFLICT (source, external_id) DO NOTHING
        RETURNING id
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (_papers_as_json(rows),))
            inserted_ids = [row[0] for row in cur.fetchall()]

    return SavePapersResult(
        total=len(rows),
        inserted=len(inserted_ids),
        skipped=len(rows) - len(inserted_ids),
        inserted_ids=inserted_ids,
    )


def get_new_papers(
    since: Optional[str] = None,
    limit: int = 100,
    source: Optional[str] = None,
) -> list[PaperOut]:
    """아직 분석(paper_analysis)되지 않은 신규 논문을 반환한다.

    papers LEFT JOIN paper_analysis 에서 분석 결과가 없는 행만 고른다.
    (paper_analysis는 paper_id UNIQUE이므로 논문당 분석은 최대 1건)

    Args:
        since: ISO8601 날짜/시각 문자열. 이 시점 이후 collected_at 분만.
            None이면 전체 미분석분.
        limit: 최대 반환 개수.
        source: 'pubmed' 등 수집처 필터. None이면 전체 수집처.

    Returns:
        PaperOut 리스트. published_date DESC 정렬 (발행일이 없는 논문은 뒤로 보내고,
        동점은 collected_at DESC → id 순으로 깨서 순서를 고정한다 — 같은 limit으로
        다시 불러도 같은 결과가 나온다). (schemas.py 참고)
    """
    if limit <= 0:
        return []

    # LEFT JOIN 후 a.paper_id IS NULL → 분석 결과가 아직 없는 논문만 남는다.
    # since/source는 NULL이면 조건을 통째로 무시한다 (SQL 한 벌로 네 경우를 모두 처리).
    # 마지막 정렬 키 p.id는 필수다: save_papers()가 배치를 한 INSERT로 넣어서
    # 같은 배치의 collected_at이 전부 동일하고, published_date도 겹치는 논문이 많다.
    # 유니크한 키로 동점을 깨지 않으면 limit을 준 결과의 순서가 호출마다 달라진다.
    sql = """
        SELECT p.id, p.source, p.external_id, p.title,
               p.abstract, p.published_date, p.url, p.collected_at
          FROM papers AS p
          LEFT JOIN paper_analysis AS a ON a.paper_id = p.id
         WHERE a.paper_id IS NULL
           AND (%(since)s::timestamptz IS NULL OR p.collected_at >= %(since)s::timestamptz)
           AND (%(source)s::text IS NULL OR p.source = %(source)s::text)
         ORDER BY p.published_date DESC NULLS LAST, p.collected_at DESC, p.id
         LIMIT %(limit)s
    """
    params = {"since": since, "source": source, "limit": limit}

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [PaperOut(**row) for row in rows]


def get_papers_by_ids(paper_ids: list[UUID]) -> list[PaperDetail]:
    """papers.id 목록으로 논문 상세를 받아온다. (입력 순서 그대로 반환)

    search_similar()가 돌려준 paper_id로 본문·메타데이터를 마저 가져오는 용도다.
    AI 쪽이 결과에 검색 점수를 순서대로 붙이기 때문에 **입력 순서를 반드시 유지**한다.
    (unnest(...) WITH ORDINALITY로 입력 순번을 붙여 그 순번으로 정렬한다.)

    Args:
        paper_ids: papers.id(UUID) 목록. 빈 목록이면 빈 결과.
            같은 id가 여러 번 들어오면 그 횟수만큼, 준 위치 그대로 돌려준다.

    Returns:
        PaperDetail 리스트. **DB에 없는 id는 결과에서 그냥 빠진다**
        (예외를 던지지 않는다). 따라서 len(결과) < len(paper_ids)일 수 있고,
        호출한 쪽에서 paper_id로 맞춰보면 어떤 id가 빠졌는지 알 수 있다.
        빠진 id는 경고 로그로도 남긴다.
    """
    if not paper_ids:
        return []

    # WITH ORDINALITY: 입력 배열에 1,2,3... 순번을 붙여준다. 그 순번으로 정렬해야
    # 유사도 순서(AI 쪽이 점수를 붙이는 순서)가 DB 반환 순서에 흔들리지 않는다.
    sql = """
        SELECT p.id AS paper_id, p.external_id, p.title, p.abstract, p.journal,
               p.published_date, p.publication_types, p.mesh_terms, p.doi
          FROM unnest(%s::uuid[]) WITH ORDINALITY AS q(id, ord)
          JOIN papers AS p ON p.id = q.id
         ORDER BY q.ord
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, ([str(pid) for pid in paper_ids],))
            rows = cur.fetchall()

    found = {row["paper_id"] for row in rows}
    missing = [str(pid) for pid in paper_ids if pid not in found]
    if missing:
        logger.warning("get_papers_by_ids: papers에 없는 id %d개: %s", len(missing), ", ".join(missing))

    return [PaperDetail(**row) for row in rows]


def get_papers_missing_metadata(
    limit: int = 500,
    source: Optional[str] = None,
) -> list[PaperOut]:
    """메타데이터가 한 번도 채워진 적 없는 논문을 반환한다. (백필 대상 고르기)

    002 마이그레이션으로 컬럼이 생기기 전에 저장된 논문은 네 컬럼이 모두 비어 있다.
    journal/doi/publication_types/mesh_terms가 **전부** 비어 있는 논문만 고른다
    (DOI 없는 논문처럼 일부만 비는 경우는 정상이라 대상에서 제외).

    팀 규칙상 스크립트가 raw SQL을 쓸 수 없어서, 백필 스크립트가 대상을 고를 때
    쓰라고 데이터 계층에 둔 함수다. 같은 스크립트를 다시 돌려도 이미 채워진 논문은
    대상에서 빠진다.

    Args:
        limit: 최대 반환 개수.
        source: 'pubmed' 등 수집처 필터. None이면 전체.

    Returns:
        PaperOut 리스트 (collected_at DESC → id 순).
    """
    if limit <= 0:
        return []

    sql = """
        SELECT p.id, p.source, p.external_id, p.title,
               p.abstract, p.published_date, p.url, p.collected_at
          FROM papers AS p
         WHERE p.journal IS NULL
           AND p.doi IS NULL
           AND coalesce(array_length(p.publication_types, 1), 0) = 0
           AND coalesce(array_length(p.mesh_terms, 1), 0) = 0
           AND (%(source)s::text IS NULL OR p.source = %(source)s::text)
         ORDER BY p.collected_at DESC, p.id
         LIMIT %(limit)s
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, {"source": source, "limit": limit})
            rows = cur.fetchall()

    return [PaperOut(**row) for row in rows]


def update_paper_metadata(papers: list[PaperIn]) -> UpdateMetadataResult:
    """이미 저장된 논문의 메타데이터(journal/doi/publication_types/mesh_terms)를 채운다.

    save_papers()는 (source, external_id)가 겹치면 기존 행을 건드리지 않고 건너뛴다.
    그 동작은 다른 파트가 전제로 쓰고 있어 바꿀 수 없으므로, 002 마이그레이션으로
    새로 생긴 컬럼을 기존 행에 채우려면 이 함수가 따로 필요하다.

    제목·초록·발행일·URL은 건드리지 않는다. 새 값이 비어 있으면(NULL / 빈 배열)
    기존 값을 그대로 둔다 — 다시 수집했을 때 값이 지워지지 않게 하기 위함이다.
    papers에 없는 논문은 새로 넣지 않는다 (새로 넣는 것은 save_papers()의 몫).

    Args:
        papers: 수집기가 만든 PaperIn 목록. (source, external_id)로 기존 행을 찾는다.

    Returns:
        UpdateMetadataResult — 시도 건수 / 갱신된 행 수 / papers에 없던 external_id 목록.
    """
    unique: dict[tuple[str, str], PaperIn] = {}
    for paper in papers:
        unique.setdefault((paper.source, paper.external_id), paper)
    rows = list(unique.values())

    if not rows:
        return UpdateMetadataResult(total=0, updated=0, not_found=[])

    # coalesce / array_length 조건: 새로 받은 값이 비어 있으면 기존 값을 유지한다.
    sql = f"""
        UPDATE papers AS p
           SET journal = coalesce(x.journal, p.journal),
               doi     = coalesce(x.doi, p.doi),
               publication_types = CASE
                   WHEN coalesce(array_length(x.publication_types, 1), 0) > 0
                   THEN x.publication_types ELSE p.publication_types END,
               mesh_terms = CASE
                   WHEN coalesce(array_length(x.mesh_terms, 1), 0) > 0
                   THEN x.mesh_terms ELSE p.mesh_terms END
          FROM jsonb_to_recordset(%s::jsonb) AS x({_PAPER_COLUMNS})
         WHERE p.source = x.source
           AND p.external_id = x.external_id
        RETURNING p.external_id
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (_papers_as_json(rows),))
            updated_ids = {row[0] for row in cur.fetchall()}

    not_found = [r.external_id for r in rows if r.external_id not in updated_ids]
    return UpdateMetadataResult(total=len(rows), updated=len(updated_ids), not_found=not_found)


def save_summary(analysis: AnalysisIn) -> UUID:
    """AI 배치가 만든 논문 분석 결과를 저장한다.

    paper_analysis에 upsert 한다 (paper_id UNIQUE → 재분석하면 기존 행을 갱신하고
    generated_at을 갱신 시각으로 다시 찍는다. 행이 새로 생기지 않으므로 id는 그대로다).
    저장이 끝나면 그 논문은 get_new_papers()의 '미분석' 목록에서 빠진다.

    analysis.embedding이 있으면 paper_embeddings에도 같은 트랜잭션으로 함께 upsert 한다
    (둘 다 성공하거나 둘 다 취소된다). embedding이 없으면 paper_embeddings는 건드리지 않는다.

    ⚠️ 임베딩 경로는 구현만 해두고 아직 실 데이터로 검증하지 않았다.
      001_init.sql의 vector(768)과 config.embedding_dim(1536)이 아직 어긋나 있어서,
      차원이 맞지 않는 벡터를 주면 DB가 거부한다(pgvector 차원 오류).
      AI 담당이 임베딩 모델을 확정해 차원을 맞춘 뒤에 검증한다.

    Args:
        analysis: 근거분류(study_type/evidence_level/guideline_relation) +
            구조화 요약(finding/comparison/limitation) + 태그, 선택적 임베딩.
            (schemas.AnalysisIn)

    Returns:
        저장된 paper_analysis row의 id (UUID).

    Raises:
        ValueError: embedding을 주면서 embedding_model을 주지 않은 경우.
            (paper_embeddings.model_name이 NOT NULL이라 모델 이름이 반드시 필요하다.)
    """
    if analysis.embedding is not None and not analysis.embedding_model:
        raise ValueError("embedding을 저장하려면 embedding_model도 함께 주어야 합니다.")

    # EXCLUDED = INSERT 하려던 새 값. 충돌하면 기존 행을 새 값으로 덮어쓴다.
    # generated_at도 now()로 다시 찍어 '언제 재분석했는지'가 남게 한다.
    analysis_sql = """
        INSERT INTO paper_analysis (
            paper_id, study_type, evidence_level, guideline_relation,
            summary_finding, summary_comparison, summary_limitation, tags, model_name
        )
        VALUES (
            %(paper_id)s, %(study_type)s, %(evidence_level)s, %(guideline_relation)s,
            %(summary_finding)s, %(summary_comparison)s, %(summary_limitation)s,
            %(tags)s, %(model_name)s
        )
        ON CONFLICT (paper_id) DO UPDATE
           SET study_type         = EXCLUDED.study_type,
               evidence_level     = EXCLUDED.evidence_level,
               guideline_relation = EXCLUDED.guideline_relation,
               summary_finding    = EXCLUDED.summary_finding,
               summary_comparison = EXCLUDED.summary_comparison,
               summary_limitation = EXCLUDED.summary_limitation,
               tags               = EXCLUDED.tags,
               model_name         = EXCLUDED.model_name,
               generated_at       = now()
        RETURNING id
    """
    params = {
        "paper_id": analysis.paper_id,
        "study_type": analysis.study_type,
        "evidence_level": analysis.evidence_level,
        "guideline_relation": analysis.guideline_relation,
        "summary_finding": analysis.summary_finding,
        "summary_comparison": analysis.summary_comparison,
        "summary_limitation": analysis.summary_limitation,
        "tags": analysis.tags,
        "model_name": analysis.model_name,
    }

    # paper_embeddings는 paper_id가 PRIMARY KEY라 같은 논문을 다시 넣으면 갱신된다.
    embedding_sql = """
        INSERT INTO paper_embeddings (paper_id, embedding, model_name)
        VALUES (%(paper_id)s, %(embedding)s, %(model_name)s)
        ON CONFLICT (paper_id) DO UPDATE
           SET embedding  = EXCLUDED.embedding,
               model_name = EXCLUDED.model_name,
               created_at = now()
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(analysis_sql, params)
            analysis_id = cur.fetchone()[0]

            if analysis.embedding is not None:
                # Vector()로 감싸야 pgvector 타입으로 넘어간다 (list 그대로면 배열로 해석됨).
                cur.execute(
                    embedding_sql,
                    {
                        "paper_id": analysis.paper_id,
                        "embedding": Vector(analysis.embedding),
                        "model_name": analysis.embedding_model,
                    },
                )

    logger.info(
        "save_summary: paper_id=%s 저장 (analysis_id=%s, embedding=%s)",
        analysis.paper_id,
        analysis_id,
        "있음" if analysis.embedding is not None else "없음",
    )
    return analysis_id


def get_papers_without_embedding(
    model_name: str,
    limit: int = 100,
) -> list[PaperOut]:
    """지정한 모델로 임베딩된 적 없는 논문을 반환한다. (임베딩 배치 대상 고르기)

    다음 두 경우를 모두 돌려준다.
      1) paper_embeddings에 아예 행이 없는 논문
      2) 행은 있지만 다른 model_name으로 저장된 논문 (모델을 바꿔서 다시 만들어야 하는 논문)

    ★ 요약(paper_analysis)과는 무관하다. 요약이 끝났는지 여부를 보지 않으므로
      임베딩 배치와 요약 배치를 서로 기다리지 않고 따로 돌릴 수 있다.
      (요약 대상을 고르는 함수는 get_new_papers()다.)

    Args:
        model_name: 기준이 되는 임베딩 모델 이름 (예: 'bge-m3').
            이 이름으로 이미 저장된 논문만 결과에서 빠진다.
        limit: 최대 반환 개수.

    Returns:
        PaperOut 리스트 (임베딩에 쓸 title / abstract 포함).
        정렬은 get_new_papers()와 같다 — published_date DESC NULLS LAST →
        collected_at DESC → id. 마지막 id 키가 있어야 같은 limit으로 다시 불러도
        순서가 흔들리지 않는다 (한 배치는 collected_at이 전부 같다).
    """
    if limit <= 0:
        return []

    # LEFT JOIN 후 e.paper_id IS NULL → 임베딩이 없는 논문,
    # e.model_name <> model_name → 다른 모델로 저장된 논문. 둘 다 대상이다.
    sql = """
        SELECT p.id, p.source, p.external_id, p.title,
               p.abstract, p.published_date, p.url, p.collected_at
          FROM papers AS p
          LEFT JOIN paper_embeddings AS e ON e.paper_id = p.id
         WHERE e.paper_id IS NULL
            OR e.model_name <> %(model_name)s
         ORDER BY p.published_date DESC NULLS LAST, p.collected_at DESC, p.id
         LIMIT %(limit)s
    """

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, {"model_name": model_name, "limit": limit})
            rows = cur.fetchall()

    return [PaperOut(**row) for row in rows]


def save_embeddings(
    items: list[tuple[UUID, list[float]]] | list[EmbeddingIn],
    model_name: str,
) -> SaveEmbeddingsResult:
    """논문 임베딩을 paper_embeddings에 저장한다. (논문당 1건, 있으면 덮어쓰기)

    ★ paper_analysis(요약) 테이블은 전혀 건드리지 않는다. 요약과 임베딩을
      따로 돌릴 수 있게 분리해 둔 경로다. (요약 저장은 save_summary())

    차원 검사를 DB에 보내기 **전에** 한다. 다른 모델(예: 1536차원 OpenAI)로 만든
    벡터가 한 건이라도 섞여 있으면 아무것도 저장하지 않고 ValueError를 던진다 —
    배치 절반만 저장돼서 모델이 섞이는 사고를 막기 위함이다.

    Args:
        items: (paper_id, 벡터) 쌍의 목록. schemas.EmbeddingIn 객체를 줘도 된다.
            벡터 길이는 반드시 EMBEDDING_DIM(= 1024, bge-m3)이어야 한다.
            같은 paper_id가 배치 안에 여러 번 있으면 **첫 건만** 쓰고 경고 로그를 남긴다
            (save_papers()와 같은 규칙. 한 INSERT에서 같은 행을 두 번 고칠 수 없다).
        model_name: 이 배치를 만든 임베딩 모델 이름 (예: 'bge-m3'). 기록용이고,
            get_papers_without_embedding()이 '다른 모델로 저장된 논문'을 고를 때 쓴다.
            paper_embeddings.model_name이 NOT NULL이라 빈 값은 받지 않는다.

    Returns:
        SaveEmbeddingsResult — 시도 건수 / 저장·갱신된 건수 /
        papers에 없어서 저장하지 못한 paper_id 목록.

    Raises:
        ValueError: model_name이 비었거나, 벡터 길이가 EMBEDDING_DIM과 다른 경우.
    """
    if not model_name or not model_name.strip():
        raise ValueError("model_name은 비울 수 없습니다 (paper_embeddings.model_name이 NOT NULL).")

    # 1) 입력을 EmbeddingIn으로 정규화 — 이 과정에서 차원 검사가 함께 일어난다.
    #    한 건이라도 어긋나면 여기서 멈추므로 DB에는 아무것도 들어가지 않는다.
    rows: list[EmbeddingIn] = []
    for item in items:
        if isinstance(item, EmbeddingIn):
            rows.append(item)
            continue
        paper_id, embedding = item
        if len(embedding) != EMBEDDING_DIM:
            # paper_id를 함께 알려 줘야 어느 논문이 문제인지 바로 찾을 수 있다.
            raise ValueError(
                f"paper_id={paper_id}: 임베딩 차원이 맞지 않습니다: "
                f"{len(embedding)} (기대값 {EMBEDDING_DIM}). "
                "bge-m3가 아닌 모델의 벡터가 섞이지 않았는지 확인하세요."
            )
        rows.append(EmbeddingIn(paper_id=paper_id, embedding=embedding))

    # 2) 배치 내 중복 paper_id 제거 (첫 건 우선).
    #    ON CONFLICT DO UPDATE는 한 명령에서 같은 행을 두 번 고칠 수 없다.
    unique: dict[UUID, EmbeddingIn] = {}
    for row in rows:
        unique.setdefault(row.paper_id, row)
    if len(unique) < len(rows):
        logger.warning(
            "save_embeddings: 배치 안에 중복 paper_id %d건, 첫 건만 저장합니다.",
            len(rows) - len(unique),
        )
    rows = list(unique.values())

    if not rows:
        return SaveEmbeddingsResult(total=0, saved=0, not_found=[])

    # 3) 한 번의 INSERT로 저장한다 (왕복 1회).
    #    벡터는 JSON에 pgvector 리터럴 문자열('[1,2,3]')로 담아 보내고 ::vector로 되돌린다.
    #    (jsonb_to_recordset은 vector 타입을 바로 만들 수 없다.)
    #    WHERE EXISTS: papers에 없는 paper_id는 넣지 않고 건너뛴다. 이 조건이 없으면
    #    외래키 위반으로 배치 전체가 실패해서, 멀쩡한 나머지 논문까지 저장되지 않는다.
    #    paper_id가 PRIMARY KEY라 ON CONFLICT DO UPDATE로 논문당 1건만 유지된다.
    sql = """
        INSERT INTO paper_embeddings (paper_id, embedding, model_name)
        SELECT x.paper_id, x.embedding::vector, %(model_name)s
          FROM jsonb_to_recordset(%(items)s::jsonb) AS x(paper_id uuid, embedding text)
         WHERE EXISTS (SELECT 1 FROM papers AS p WHERE p.id = x.paper_id)
        ON CONFLICT (paper_id) DO UPDATE
           SET embedding  = EXCLUDED.embedding,
               model_name = EXCLUDED.model_name,
               created_at = now()
        RETURNING paper_id
    """
    payload = json.dumps(
        [
            {"paper_id": str(row.paper_id), "embedding": _as_vector_literal(row.embedding)}
            for row in rows
        ]
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"items": payload, "model_name": model_name})
            saved_ids = {r[0] for r in cur.fetchall()}

    not_found = [row.paper_id for row in rows if row.paper_id not in saved_ids]
    if not_found:
        logger.warning(
            "save_embeddings: papers에 없는 paper_id %d개, 저장하지 않았습니다: %s",
            len(not_found),
            ", ".join(str(pid) for pid in not_found),
        )
    logger.info(
        "save_embeddings: %d건 저장/갱신 (model_name=%s, 건너뜀 %d건)",
        len(saved_ids),
        model_name,
        len(not_found),
    )
    return SaveEmbeddingsResult(total=len(rows), saved=len(saved_ids), not_found=not_found)


def search_similar(
    embedding: list[float],
    top_k: int = 5,
    model_name: Optional[str] = None,
) -> list[SearchResult]:
    """질의 임베딩과 유사한 논문을 pgvector 코사인 거리 기준으로 검색한다.

    paper_embeddings를 검색하고 papers / paper_analysis를 조인해 돌려준다.

    Args:
        embedding: 질의 벡터. 길이는 001_init.sql의 vector(N)과 일치해야 한다.
        top_k: 반환할 개수.
        model_name: 임베딩 모델 필터. 여러 모델이 섞여 있을 때 같은 모델끼리만
            비교하기 위해 쓴다. None이면 전체.

    Returns:
        SearchResult 리스트 (score 내림차순).
    """
    raise NotImplementedError
