"""검색 방식(mode="search") 검색어용 한→영 단어표 (박주현 담당).

김현서 님 점검(2026-10): 한국어 한 줄 검색어는 관련 0/5, 영어로 증상별로 쪼개면 4/5.
영어 검색어에 caregiver/family를 넣으면 보호자 본인 논문이 끌려와 2/5로 떨어진다.
→ 단어표 값 말고는 caregiver/family를 검색어에 넣지 않는다.

찾을 때는 앞뒤 공백·띄어쓰기를 무시한다 ("수면장애" = "수면 장애"). 표에 없는 단어는 한국어 그대로 둔다.
"""
from typing import Optional

# 단계 (DEMENTIA_STAGES 4개 전부)
STAGE_EN = {
    "경도인지장애": "mild cognitive impairment",
    "경도": "mild dementia",
    "중등도": "moderate dementia",
    "중증": "severe dementia",
}

# 증상 (시드 14개 + 자주 나올 것)
SYMPTOM_EN = {
    "기억력 저하": "memory loss",
    "반복 질문": "repetitive questioning",
    "길 찾기 어려움": "spatial disorientation",
    "약 복용 잊음": "medication non-adherence",
    "수면장애": "sleep disturbance",
    "수면 문제": "sleep disturbance",
    "낮잠 증가": "excessive daytime sleepiness",
    "야간 불안": "nighttime agitation and sundowning",
    "배회": "wandering",
    "낙상 위험": "fall risk",
    "거동 불편": "impaired mobility",
    "삼킴 곤란": "dysphagia",
    "공격성": "aggression",
    "우울": "depression",
    "무기력": "apathy",
    # 추가 후보
    "불안": "anxiety",
    "초조": "agitation",
    "환각": "hallucinations",
    "망상": "delusions",
    "의심": "suspiciousness",
    "식욕 저하": "loss of appetite",
    "요실금": "urinary incontinence",
    "언어 장애": "language difficulties",
}

# 관심사 (시드 13개 + 추가)
INTEREST_EN = {
    "수면 위생": "sleep hygiene",
    "운동": "physical exercise",
    "인지 훈련": "cognitive training",
    "인지 활동": "cognitive stimulation activities",
    "배회 대응": "wandering management",
    "낙상 예방": "fall prevention",
    "복약 관리": "medication management",
    "우울 관리": "depression management",
    "식사 보조": "feeding assistance",
    "욕창 예방": "pressure ulcer prevention",
    "주간보호센터": "adult day care",
    "요양시설": "nursing home care",
    "보호자 지원 프로그램": "caregiver support programs",
    # 추가 후보
    "약물 치료": "pharmacological treatment",
    "비약물 치료": "non-pharmacological interventions",
    "음악 치료": "music therapy",
    "회상 치료": "reminiscence therapy",
}

# 한 줄 대신 여러 줄로 펼치는 관심사. {stage} 자리에 단계 영어가 들어간다.
# 김현서 님 P3 실험(scripts/eval_p3_queries.py)의 "D_치료이름_4줄"을 약물/비약물로 나눈 것.
INTEREST_EXPAND = {
    "약물 치료": [
        "cholinesterase inhibitors and memantine for {stage}",
        "anti-amyloid antibody treatment (lecanemab, donanemab) for early Alzheimer's disease",
    ],
    "비약물 치료": [
        "cognitive stimulation and cognitive training for {stage}",
        "physical exercise intervention for {stage} and mild cognitive impairment",
    ],
}

# 초기 단계(경도인지장애·경도)일 때만 넣는 펼친 줄. 항아밀로이드 항체는 초기 알츠하이머 대상이다.
EARLY_STAGE_ONLY = {"anti-amyloid antibody treatment (lecanemab, donanemab) for early Alzheimer's disease"}
EARLY_STAGES = {STAGE_EN["경도인지장애"], STAGE_EN["경도"]}


def _key(text: str) -> str:
    return "".join(text.split())


def _lookup(table: dict, text: str) -> Optional[object]:
    """띄어쓰기를 무시하고 표에서 찾는다. 없으면 None."""
    return {_key(k): v for k, v in table.items()}.get(_key(text))


def to_en(table: dict[str, str], text: str, default: Optional[str] = None) -> str:
    """표에 있으면 영어, 없으면 default (주지 않으면 한국어 그대로, 앞뒤 공백만 뺀다)."""
    return _lookup(table, text) or (default if default is not None else text.strip())


def expand_interest(text: str, stage_en: str) -> Optional[list[str]]:
    """INTEREST_EXPAND에 있으면 펼친 줄 목록({stage}를 채우고, 초기 단계 전용 줄은 초기 단계일 때만), 없으면 None."""
    lines = _lookup(INTEREST_EXPAND, text)
    if lines is None:
        return None
    return [line.format(stage=stage_en) for line in lines
            if line not in EARLY_STAGE_ONLY or stage_en in EARLY_STAGES]
