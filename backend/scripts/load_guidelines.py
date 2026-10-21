"""가이드라인 PDF → 장·절 조각 → 임베딩 → guideline_knowledge 저장 (담당: 김현서).

자료 지도 (주현님이 정리, 재륜님 목록 기준):
  app/ai/guidelines/guideline_docs.csv      — 문서 1개 = 1줄 (doc_title, source, published_year, language, source_url)
  app/ai/guidelines/guideline_sections.csv  — 장·절마다 PDF 쪽 범위, topic, tags, 우선순위, 빼면 좋은 쪽

PDF 원문은 저작권 때문에 깃에 올리지 않는다. backend/data/guidelines/ 에 아래 이름으로 넣는다.
  1_*.pdf  나에게 힘이 되는 치매 가이드북 (펼침면 2단 → 반쪽씩 잘라서 뽑음)
  2_*.pdf  대한치매학회 지침 (쪽 범위가 아직 비어 있어 건너뜀)
  3_*.pdf  WHO iSupport (정답지 쪽 제외)
  4_*.pdf  Alzheimer's Association 권고 (참고문헌·'Downloaded from' 줄 제거)

흐름
  ① 지도에서 우선순위가 --priority(기본: 상,중)인 장·절만 고른다
  ② PDF 쪽 범위의 글자를 뽑는다 (빼면 좋은 쪽은 건너뜀)
  ③ 문단 단위로 MAX_CHARS 이하 조각으로 나눈다 → chunk_index는 문서 안에서 0부터
  ④ "문서 > 장·절" 머리말 + 본문을 embed_documents()로 임베딩
  ⑤ GuidelineChunkIn → save_guidelines()  [주현님 함수]  (source_url, chunk_index) 같으면 갱신

실행 (backend/ 에서):
    python -m scripts.load_guidelines --dry-run          # 저장 없이 조각만 만들고 미리보기 파일 저장
    python -m scripts.load_guidelines --dry-run --doc 1  # 1번 문서만
    python -m scripts.load_guidelines                    # 임베딩 + 저장 (Docker DB 필요)
    python -m scripts.load_guidelines --priority 상      # '상'만
미리보기: data/guidelines/preview_<문서번호>.txt — 글자가 깨지거나 섞이지 않았는지 눈으로 확인한다.
여러 번 실행해도 안전하다 (같은 조각은 갱신).
"""
import argparse
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
MAP_DIR = BACKEND / "app" / "ai" / "guidelines"
PDF_DIR = BACKEND / "data" / "guidelines"

MAX_CHARS = 1200   # 조각 최대 길이 (한국어 기준 약 1~2문단). 너무 길면 검색이 흐려진다
MIN_CHARS = 150    # 이보다 짧은 꼬리 조각은 앞 조각에 붙인다
SAVE_BATCH = 32

# 쪽마다 반복되는 머리말·꼬리말 줄
NOISE_RE = re.compile(
    r"^\s*("
    r"\d+"                                          # 쪽 번호만 있는 줄
    r"|Downloaded from https?://\S+.*|by guest|on \d+ \w+ \d{4}"    # 4번 다운로드 표시
    r"|(S\d+ )?The Gerontologist, \d{4}, Vol\..*"   # 4번 학술지 머리말
    r"|Chapter \d+( \d+)?|ㅣ\d+ㅣ"                   # 1번 장 머리말
    r"|troppuSi|noitnevretnI|launaM|MODULE"          # 3번 옆 띠(뒤집힌 글자)
    r"|:|https://dnd\.or\.kr.*|치매 임상 진료 지침 행동심리증상 약물치료 권고안"  # 2번 머리말·꼬리말
    r")\s*$",
    re.IGNORECASE,
)
# 4번: 장 끝 참고문헌 시작 (단 안에서 'References'로 시작하는 줄)
REFERENCES_RE = re.compile(r"^\s*References\b", re.IGNORECASE)
# 3번: 체크박스·빈칸 줄
CHECKBOX_RE = re.compile(r"^[\s☐□▢❑_.]+$")


@dataclass
class Doc:
    no: str
    title: str
    source: str
    year: int | None
    language: str
    url: str


@dataclass
class Section:
    doc_no: str
    title: str
    page_start: int | None
    page_end: int | None
    topic: str
    tags: list[str]
    priority: str
    skip_pages: set[int] = field(default_factory=set)
    half: str | None = None  # 1번 가이드북: "left" / "right" / None(양쪽 다)
    start_marker: str = ""   # 시작 쪽에서 이 글자 앞은 버린다 (한 쪽에 두 절이 걸칠 때, 2번 'KQ2:' 등)
    end_marker: str = ""     # 끝 쪽에서 이 글자부터는 버린다


@dataclass
class Chunk:
    doc: Doc
    section: Section
    index: int
    content: str


# ---------------------------------------------------------------- 지도 읽기
def _pages(text: str) -> set[int]:
    """'51–55' / '24' / '257-258, 260' → 쪽 번호 집합"""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", text or ""):
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[–-]\s*(\d+)$", part)
        if m:
            out.update(range(int(m[1]), int(m[2]) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def load_map() -> tuple[dict[str, Doc], list[Section]]:
    with open(MAP_DIR / "guideline_docs.csv", encoding="utf-8") as f:
        docs = {
            r["doc_no"]: Doc(r["doc_no"], r["doc_title"], r["source"],
                             int(r["published_year"]) if r["published_year"].isdigit() else None,
                             r["language"], r["source_url"])
            for r in csv.DictReader(f)
        }
    sections = []
    with open(MAP_DIR / "guideline_sections.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            note = r["note"]
            half = "left" if "왼쪽 반쪽" in note else "right" if "오른쪽 반쪽" in note else None
            sections.append(Section(
                doc_no=r["doc_no"],
                title=r["section"],
                page_start=int(r["page_start"]) if r["page_start"].isdigit() else None,
                page_end=int(r["page_end"]) if r["page_end"].isdigit() else None,
                topic=r["topic"],
                tags=[t.strip() for t in r["tags"].split(",") if t.strip()],
                priority=r["priority"],
                skip_pages=_pages(r["skip_pages"]),
                half=half,
                start_marker=(r.get("start_marker") or "").strip(),
                end_marker=(r.get("end_marker") or "").strip(),
            ))
    return docs, sections


# ---------------------------------------------------------------- PDF 글자 뽑기
def find_pdf(doc_no: str) -> Path | None:
    hits = sorted(PDF_DIR.glob(f"{doc_no}_*.pdf"))
    return hits[0] if hits else None


TWO_COLUMN_DOCS: set[str] = set()  # 반으로 잘라 단별로 뽑을 문서 (지금은 없음)
LAYOUT_DOCS = {"3", "4"}  # 글상자·2단이 섞인 문서 → pdfminer 배치 분석으로 뽑는다
# 2번은 한글·숫자 글꼴의 줄 높이가 달라서 기본값으로 뽑으면 띄어쓰기가 사라지고 숫자·문장부호가
# 다른 줄로 떨어진다 → 같은 줄로 보는 범위를 넓힌다
EXTRACT_KW = {"2": {"x_tolerance": 1.5, "y_tolerance": 7}}
GARBLED_DOCS = {"2"}  # 표·그림(세로 글자)이 한 글자씩 흩어져 나오는 줄을 버릴 문서


def _halves(page) -> tuple[str, str]:
    x0, top, x1, bottom = page.bbox  # 0에서 시작하지 않는 PDF도 있다 (4번)
    mid = (x0 + x1) / 2
    left = page.crop((x0, top, mid, bottom)).extract_text() or ""
    right = page.crop((mid, top, x1, bottom)).extract_text() or ""
    return left, right


def _page_text(page, doc_no: str, half: str | None) -> str:
    """1번 가이드북은 펼침면(책 2쪽)이라 반쪽씩, 4번은 2단이라 단별로 뽑는다. 나머지는 쪽 전체.

    그냥 뽑으면 왼쪽·오른쪽 글이 한 줄씩 섞인다.
    """
    if doc_no == "1" and page.width > page.height:  # 한 쪽짜리(표지 등)는 그대로
        left, right = _halves(page)
        if half == "left":
            return left
        if half == "right":
            return right
        return left + "\n\n" + right
    if doc_no in TWO_COLUMN_DOCS:
        left, right = _halves(page)
        return left + "\n\n" + right
    return page.extract_text(**EXTRACT_KW.get(doc_no, {})) or ""


def _garbled(line: str) -> bool:
    """'a 향 2 0 P 1 t 토 에' 처럼 한 글자 토막이 대부분인 줄 (회전된 표·그림)"""
    toks = line.split()
    return len(toks) >= 8 and sum(len(t) == 1 for t in toks) / len(toks) > 0.6


def clean(text: str, doc_no: str) -> str:
    lines = []
    for line in text.splitlines():
        if doc_no in GARBLED_DOCS and _garbled(line):
            continue
        if doc_no == "4" and REFERENCES_RE.match(line):
            break  # 참고문헌부터는 버린다
        if NOISE_RE.match(line) or CHECKBOX_RE.match(line):
            continue
        lines.append(line.rstrip())
    text = "\n".join(lines)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # 영어 줄끝 하이픈 이어 붙이기
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_section(pdf, sec: Section) -> str:
    pages = [no for no in range(sec.page_start, sec.page_end + 1)
             if no not in sec.skip_pages and no <= len(pdf.pages)]
    if sec.doc_no in LAYOUT_DOCS:
        # 단·글상자를 알아서 나눠 읽는 pdfminer 배치 분석 (pdfplumber가 내부에서 쓰는 라이브러리)
        from pdfminer.high_level import extract_text
        from pdfminer.layout import LAParams
        text = extract_text(pdf.stream, page_numbers=[no - 1 for no in pages], laparams=LAParams())
        return clean(text.replace("\x0c", "\n\n"), sec.doc_no)
    texts = [_page_text(pdf.pages[no - 1], sec.doc_no, sec.half) for no in pages]
    return clean(_cut("\n\n".join(texts), sec), sec.doc_no)


def _cut(text: str, sec: Section) -> str:
    """한 쪽에 두 절이 걸치면 시작·끝 표시 글자로 잘라 낸다."""
    if sec.start_marker and sec.start_marker in text:
        text = text[text.index(sec.start_marker):]
    if sec.end_marker and sec.end_marker in text:
        text = text[:text.index(sec.end_marker, len(sec.start_marker))]
    return text


# ---------------------------------------------------------------- 조각 나누기
def split_chunks(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """빈 줄(문단) 기준으로 모으다가 max_chars를 넘으면 자른다. 너무 긴 문단은 문장 단위로 자른다."""
    paras = [p.strip().replace("\n", " ") for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= max_chars:
            pieces.append(p)
            continue
        buf = ""
        for s in re.split(r"(?<=[.!?。다요])\s+", p):
            if buf and len(buf) + len(s) + 1 > max_chars:
                pieces.append(buf)
                buf = s
            else:
                buf = f"{buf} {s}".strip()
        if buf:
            pieces.append(buf)

    chunks: list[str] = []
    buf = ""
    for p in pieces:
        if buf and len(buf) + len(p) + 2 > max_chars:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}".strip()
    if buf:
        if chunks and len(buf) < MIN_CHARS:
            chunks[-1] += "\n\n" + buf
        else:
            chunks.append(buf)
    return chunks


def build_chunks(docs: dict[str, Doc], sections: list[Section], priorities: set[str],
                 only_doc: str | None) -> list[Chunk]:
    import pdfplumber  # 저장 없이 지도만 볼 때도 import 오류가 나지 않게 여기서 불러온다

    out: list[Chunk] = []
    for doc_no, doc in docs.items():
        if only_doc and doc_no != only_doc:
            continue
        secs = [s for s in sections if s.doc_no == doc_no and s.priority in priorities]
        if not secs:
            continue
        pdf_path = find_pdf(doc_no)
        if pdf_path is None:
            print(f"[{doc_no}] PDF 없음 → 건너뜀 (data/guidelines/{doc_no}_*.pdf 로 넣어 주세요)")
            continue
        index = 0  # chunk_index는 문서 안에서 0부터 (지도 순서대로)
        with pdfplumber.open(pdf_path) as pdf:
            for sec in secs:
                if sec.page_start is None or sec.page_end is None:
                    print(f"[{doc_no}] 쪽 범위 없음 → 건너뜀: {sec.title}")
                    continue
                text = extract_section(pdf, sec)
                if not text:
                    print(f"[{doc_no}] 글자 없음(그림 쪽?) → 건너뜀: {sec.title}")
                    continue
                for piece in split_chunks(text):
                    out.append(Chunk(doc, sec, index, piece))
                    index += 1
    return out


def embed_text(c: Chunk) -> str:
    """검색이 장·절 맥락을 알도록 머리말을 붙여서 임베딩한다 (DB content에는 본문만 저장)."""
    return f"{c.doc.title} > {c.section.title} ({c.section.topic})\n{c.content}"


# ---------------------------------------------------------------- 실행
def main() -> None:
    ap = argparse.ArgumentParser(description="가이드라인 적재")
    ap.add_argument("--dry-run", action="store_true", help="저장 없이 조각 미리보기만")
    ap.add_argument("--doc", help="이 번호 문서만 (예: 1)")
    ap.add_argument("--priority", default="상,중", help="넣을 우선순위 (쉼표로, 기본: 상,중)")
    a = ap.parse_args()

    docs, sections = load_map()
    priorities = {p.strip() for p in a.priority.split(",") if p.strip()}
    chunks = build_chunks(docs, sections, priorities, a.doc)
    if not chunks:
        print("만들어진 조각이 없습니다.")
        return

    # 요약
    print(f"\n{'문서':<4} {'장·절':>5} {'조각':>5} {'평균 글자':>8} {'최대':>6}")
    for doc_no in sorted({c.doc.no for c in chunks}):
        cs = [c for c in chunks if c.doc.no == doc_no]
        lens = [len(c.content) for c in cs]
        print(f"{doc_no:<4} {len({c.section.title for c in cs}):>5} {len(cs):>5} "
              f"{sum(lens) // len(lens):>8} {max(lens):>6}")
    print(f"합계 조각 {len(chunks)}개")

    if a.dry_run:
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        for doc_no in sorted({c.doc.no for c in chunks}):
            out = PDF_DIR / f"preview_{doc_no}.txt"
            with open(out, "w", encoding="utf-8") as f:
                for c in (c for c in chunks if c.doc.no == doc_no):
                    f.write(f"===== #{c.index} [{c.section.priority}] {c.section.title} "
                            f"| topic={c.section.topic} | tags={c.section.tags} | {len(c.content)}자\n")
                    f.write(c.content + "\n\n")
            print(f"미리보기: {out.relative_to(BACKEND)}")
        return

    from app.ai.retrieval.embedding import MODEL_NAME, embed_documents
    from app.db.functions import save_guidelines
    from app.schemas import GuidelineChunkIn

    saved = 0
    for i in range(0, len(chunks), SAVE_BATCH):
        batch = chunks[i:i + SAVE_BATCH]
        vecs = embed_documents([embed_text(c) for c in batch])
        items = [
            GuidelineChunkIn(
                doc_title=c.doc.title, section=c.section.title, chunk_index=c.index,
                content=c.content, topic=c.section.topic, source=c.doc.source or None,
                source_url=c.doc.url, published_year=c.doc.year,
                language=c.doc.language or None, tags=c.section.tags,
                embedding=v, embedding_model=MODEL_NAME,
            )
            for c, v in zip(batch, vecs)
        ]
        saved += save_guidelines(items)
        print(f"  {saved}/{len(chunks)}개 저장")
    print(f"\n완료: 조각 {saved}개 저장")


if __name__ == "__main__":
    main()
