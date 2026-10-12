"""근거등급 규칙 매핑 (담당: 김현서).

PubMed가 논문마다 붙여 주는 PublicationType(예: "Randomized Controlled Trial")을 보고
① study_type(연구 유형) ② evidence_level(근거 등급, 1이 가장 높음) ③ 동물 연구 여부를 정한다.

★ 최신 논문은 PubMed 색인이 늦어서 PublicationType이 "Journal Article"뿐이고 MeSH도 비어 있는
  경우가 많다. 그래서 꼬리표로 못 정하면 **제목(+초록 앞부분)의 키워드**로 한 번 더 판단한다.

근거 피라미드 (GRADE 유사, 숫자가 작을수록 강한 근거)
  1  meta_analysis / systematic_review / guideline
  2  rct
  3  clinical_trial (비무작위 임상시험)
  4  observational / qualitative
  5  review (서술적·스코핑 리뷰) / case_report
  6  expert_opinion (사설·논평·편지)
  -  protocol (결과 없음) / animal (동물·세포 연구) / other (판단 불가) → 등급 없음(None)

나중에(5~6주차) BERT 분류기로 바꿀 수 있도록 classify() 한 함수로만 노출한다.
"""
import re
from typing import NamedTuple, Optional

LEVEL = {
    "meta_analysis": 1, "systematic_review": 1, "guideline": 1,
    "rct": 2,
    "clinical_trial": 3,
    "observational": 4, "qualitative": 4,
    "review": 5, "case_report": 5,
    "expert_opinion": 6,
    "protocol": None, "animal": None, "other": None,
}

# 화면 표시용 근거 등급 (2026-10 회의에서 A~D로 결정). 1~6 등급을 묶는다.
#   A = 1 메타분석·체계적 문헌고찰·가이드라인
#   B = 2 RCT
#   C = 3~4 비무작위 임상시험·관찰연구·질적연구
#   D = 5~6 서술적 리뷰·증례보고·전문가 의견
#   None = 판단 불가·프로토콜·동물 → 화면에는 "등급 미확인"
GRADE = {1: "A", 2: "B", 3: "C", 4: "C", 5: "D", 6: "D"}

# PubMed PublicationType → study_type. 위에서부터 먼저 걸리는 것(= 더 강한 근거)을 쓴다.
PUBTYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("protocol", ("Clinical Trial Protocol",)),
    ("meta_analysis", ("Meta-Analysis", "Network Meta-Analysis")),
    ("systematic_review", ("Systematic Review",)),
    ("guideline", ("Practice Guideline", "Guideline", "Consensus Development Conference")),
    ("rct", ("Randomized Controlled Trial", "Pragmatic Clinical Trial", "Equivalence Trial")),
    ("clinical_trial", ("Controlled Clinical Trial", "Clinical Trial", "Clinical Trial, Phase I",
                        "Clinical Trial, Phase II", "Clinical Trial, Phase III", "Clinical Trial, Phase IV")),
    ("observational", ("Observational Study", "Comparative Study", "Evaluation Study",
                       "Validation Study", "Twin Study", "Multicenter Study")),
    ("case_report", ("Case Reports",)),
    ("review", ("Scoping Review", "Review", "Systematic Review Protocol")),
    ("expert_opinion", ("Editorial", "Comment", "Letter", "Expert Opinion")),
]

# 꼬리표로 못 정했을 때 ① 제목 키워드 → ② 초록의 "확실한 표현"만 (위에서부터 우선)
#   초록에는 "기존 RCT에서는…", "이전 리뷰들은…" 같은 언급이 흔해서, 제목과 같은 느슨한 규칙을 쓰면 오탐이 난다.
TITLE_RULES: list[tuple[str, re.Pattern]] = [
    ("protocol", re.compile(r"\b(study|trial) protocol\b|\bprotocol (for|of)\b", re.I)),
    ("meta_analysis", re.compile(r"\bmeta[- ]?analys", re.I)),
    ("systematic_review", re.compile(r"\bsystematic (literature )?review\b|\bumbrella review\b", re.I)),
    ("guideline", re.compile(r"\bguideline|\bconsensus (statement|recommendations?)\b|\bdelphi\b", re.I)),
    ("rct", re.compile(r"\brandomi[sz]ed\b|\bRCT\b", re.I)),
    ("clinical_trial", re.compile(r"\b(pilot|feasibility|clinical|non-?randomi[sz]ed) (trial|study)\b|\bquasi[- ]experimental\b|\bpre-?post\b", re.I)),
    ("qualitative", re.compile(r"\bqualitative\b|\binterview study\b|\bfocus groups?\b|\bthematic analysis\b|\bphenomenolog|\blived experiences?\b|\bmixed[- ]methods?\b", re.I)),
    ("observational", re.compile(
        r"\bcohort\b|\bcross[- ]sectional\b|\blongitudinal\b|\bcase[- ]control\b|\bobservational\b|\bregistry\b|"
        r"\bsurvey\b|\bretrospective\b|\bprospective\b|\bsecondary analysis\b|\bassociat(ed|ion|ions)\b|"
        r"\bpsychometric|\bvalidation\b|\breliability\b|\bprevalence\b|\bincidence\b|\brisk factors?\b|\bpredictors?\b", re.I)),
    ("review", re.compile(r"\breview\b|\boverview\b", re.I)),
    ("case_report", re.compile(r"\bcase (report|series)\b", re.I)),
    ("expert_opinion", re.compile(r"\b(editorial|commentary|perspective|viewpoint)\b", re.I)),
]
ABSTRACT_RULES: list[tuple[str, re.Pattern]] = [
    ("protocol", re.compile(r"\bthis (study )?protocol\b|\bwill be (randomi[sz]ed|recruited)\b", re.I)),
    ("meta_analysis", re.compile(r"\b(we|this) (conducted |performed )?(a )?(systematic review and )?meta[- ]?analys|\bpooled (analysis|estimates?)\b", re.I)),
    ("systematic_review", re.compile(r"\b(we|this) (conducted |performed )?(a )?systematic review\b|\bPRISMA\b", re.I)),
    ("rct", re.compile(r"\b(were |was )?randomly (assigned|allocated)\b|\bwere randomi[sz]ed (to|into)\b", re.I)),
    ("qualitative", re.compile(r"\bsemi-?structured interviews?\b|\bthematic(ally)? analy[sz]|\bfocus groups?\b|\bqualitative (study|design|approach|data)\b", re.I)),
    ("observational", re.compile(r"\b(cohort|cross-sectional|case-control|retrospective|prospective|longitudinal|observational) (study|design|analysis|data)\b|\bsecondary analysis\b", re.I)),
]

# RCT 데이터를 "재사용"한 2차 분석 논문은 PubMed가 RCT 꼬리표를 그대로 붙이는 경우가 많다.
# 제목에 무작위 배정 표현은 없고 "~와 관련 있다", "2차 분석" 같은 관찰연구 표현이 있으면 observational로 낮춘다.
SECONDARY_RE = re.compile(
    r"\bassociat(ed|ion|ions)\b|\bsecondary analysis\b|\bpost[- ]hoc\b|\bexploratory analysis\b|"
    r"\bbaseline (data|characteristics)\b|\bcross[- ]sectional\b|\bpredictors?\b|\bcorrelates?\b", re.I
)
TRIAL_IN_TITLE_RE = re.compile(r"\brandomi[sz]ed\b|\bRCT\b|\btrial\b", re.I)

ANIMAL_RE = re.compile(
    r"\b(mice|mouse|murine|rats?|rodents?|zebrafish|drosophila|c\. elegans|primates?|"
    r"transgenic|knock-?out|in vitro|cell lines?|organoids?)\b", re.I
)
# "adult male mice"처럼 동물에도 쓰는 단어(adult, male 등)는 넣지 않는다
HUMAN_RE = re.compile(r"\b(patients?|participants?|people|persons?|caregivers?|carers?|residents?|older adults|nursing home)\b", re.I)


# ---- 치매 관련 논문인지 (검색 결과에서 뇌졸중·파킨슨 등 다른 질환 논문을 빼기 위함) ----
# 수집 검색식에 "Cognitive Dysfunction"[MeSH]가 있어서 뇌졸중 후 인지장애 같은 논문도 섞여 들어온다.
# 골드셋 901편으로 확인: 이 규칙으로 32편이 빠지고, 그중 관련(1·2점) 논문은 0편이었다.
DEMENTIA_MESH_RE = re.compile(r"\b(Dementia|Alzheimer Disease|Lewy Body Disease|Frontotemporal)", re.I)
DEMENTIA_RE = re.compile(
    r"\b(dementias?|alzheimer|neurocognitive disorders?|lewy bod(?:y|ies)|frontotemporal|"
    r"mild cognitive impairment|MCI|BPSD|memory clinic)", re.I
)
COGNITIVE_RE = re.compile(r"\b(cognitive impairment|cognitively impaired|cognitive decline|cognitive dysfunction)\b", re.I)
OTHER_DISEASE_RE = re.compile(
    r"\b(stroke|parkinson|huntington|multiple sclerosis|HIV|cancer|chemotherap|schizophreni|bipolar|"
    r"traumatic brain|TBI|epilep|diabet|kidney|renal|hemodialysis|haemodialysis|cardiac|heart failure|"
    r"delirium|surgery|anesthe|COVID)", re.I
)


def is_dementia_related(mesh_terms: list[str] | None, title: str = "", abstract: str | None = None) -> bool:
    """치매·MCI 관련 논문이면 True.

    ① MeSH에 Dementia / Alzheimer Disease 등이 있으면 True
    ② 제목이 다른 질환(뇌졸중·파킨슨·헌팅턴 등)인데 제목에 치매 단어가 없으면 False
    ③ 제목+초록에 치매 단어(dementia, Alzheimer, MCI 등)가 있으면 True
    ④ "cognitive impairment"만 있으면 제목에 다른 질환이 없을 때만 True
    """
    if DEMENTIA_MESH_RE.search(" ".join(mesh_terms or [])):
        return True
    title = title or ""
    if OTHER_DISEASE_RE.search(title) and not DEMENTIA_RE.search(title):
        return False
    text = f"{title} {abstract or ''}"
    if DEMENTIA_RE.search(text):
        return True
    return bool(COGNITIVE_RE.search(text))


class Evidence(NamedTuple):
    study_type: str               # 위 LEVEL의 키 중 하나
    evidence_level: Optional[int] # 1(강함) ~ 6(약함), 판단 불가면 None
    is_animal: bool               # 동물·세포 연구면 True (보호자 피드에서 제외 대상)
    source: str                   # "pubtype" / "pubtype+title" / "keyword" / "none" — 어떻게 판단했는지 (디버깅용)


def _is_animal(mesh_terms: list[str], text: str) -> bool:
    mesh = {m.lower() for m in mesh_terms or []}
    if "animals" in mesh and "humans" not in mesh:
        return True
    if "humans" in mesh:
        return False
    # MeSH가 없으면(최신 논문) 제목+초록 앞부분으로 판단: 동물 단어는 있는데 사람 단어는 없을 때만
    return bool(ANIMAL_RE.search(text)) and not HUMAN_RE.search(text)


def classify(
    publication_types: list[str] | None,
    mesh_terms: list[str] | None = None,
    title: str = "",
    abstract: str | None = None,
) -> Evidence:
    """PublicationType(+MeSH, 제목, 초록)으로 연구 유형과 근거 등급을 정한다."""
    pubtypes = set(publication_types or [])
    text = f"{title or ''} {(abstract or '')[:600]}"

    if _is_animal(mesh_terms or [], text):
        return Evidence("animal", None, True, "pubtype" if mesh_terms else "keyword")

    for study_type, names in PUBTYPE_RULES:
        if pubtypes.intersection(names):
            if (study_type in ("rct", "clinical_trial") and SECONDARY_RE.search(title or "")
                    and not TRIAL_IN_TITLE_RE.search(title or "")):
                return Evidence("observational", LEVEL["observational"], False, "pubtype+title")
            return Evidence(study_type, LEVEL[study_type], False, "pubtype")

    for study_type, pattern in TITLE_RULES:
        if pattern.search(title or ""):
            return Evidence(study_type, LEVEL[study_type], False, "keyword")

    for study_type, pattern in ABSTRACT_RULES:
        if pattern.search(abstract or ""):
            return Evidence(study_type, LEVEL[study_type], False, "keyword")

    return Evidence("other", None, False, "none")


def to_grade(evidence_level: Optional[int]) -> Optional[str]:
    """1~6 근거 등급 → 화면 표시용 A~D. 등급이 없으면(None) None을 돌려준다(= 등급 미확인).

    예) to_grade(classify(...).evidence_level)  →  "B"
        to_grade(None)                           →  None
    """
    return GRADE.get(evidence_level)
