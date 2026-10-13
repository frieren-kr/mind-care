"""논문별 3칸 요약 + 한국어 제목을 LLM으로 미리 만들어 paper_analysis에 저장한다. (담당: 박주현)

흐름 (논문 한 편씩):
  ① get_new_papers(limit)   — 요약이 아직 없는 논문 (초록이 없으면 건너뛴다)
  ② OpenAI (JSON 형식 강제) — title_ko / summary_background / summary_finding / summary_limitation
  ③ (--polish를 줄 때만) OpenAI 한 번 더 — 모를 만한 단어를 쉬운 말로 다듬는다. 기본은 끔 (토큰이 약 3배)
  ④ 자동 검사 check()        — 통계 표기·영어 단어·깨진 단어·금지 단어·약 이름 영어 괄호 빠짐이 있으면
                               한 번만 다시 생성, 또 걸리면 실패
  ⑤ save_summary()          — study_type / evidence_level은 보내지 않으므로 기존 값이 유지된다

저장된 논문은 ①에서 빠지므로, 중간에 끊겨도 다시 실행하면 남은 것만 이어서 한다.

실행 (backend/ 에서, Docker DB가 떠 있어야 함. .env에 OPENAI_API_KEY, OPENAI_SUMMARY_MODEL 필요):
    python -m scripts.summarize_papers --limit 20 --dry-run --out ../../summaries.md   # 저장 없이 결과만 파일로
    python -m scripts.summarize_papers --limit 20                                      # 20편 저장
    python -m scripts.summarize_papers --paper-ids <uuid> <uuid>                       # 지정한 논문만 (다시 만들기 포함)
    python -m scripts.summarize_papers --limit 20 --polish                             # 두 번째 다듬기까지
"""
import argparse
import re
import statistics
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.db import functions
from app.schemas import AnalysisIn

TIMEOUT_SECONDS = 120
# 추론형 모델은 생각하는 데 쓰는 토큰도 이 한도에 포함된다. 응답이 잘려 실패하면 올린다.
MAX_TOKENS = 4000
MAX_RETRIES = 2  # 첫 시도 뒤 다시 시도하는 횟수

NO_BACKGROUND = "연구 배경에 대한 설명은 따로 없습니다"
NO_LIMITATION = "이 연구의 한계에 대해서는 따로 밝힌 내용이 없습니다."

# 처음 쓰기(SYSTEM_PROMPT)와 두 번째 다듬기(POLISH_PROMPT)가 같이 쓰는 쉬운 말 규칙.
EASY_WORD_RULES = """\
[독자와 난이도]
- 독자는 치매 환자를 돌보는 가족 보호자입니다. 의학·통계 지식이 전혀 없습니다.
- 전문 의학 뉴스처럼 쓰지 않습니다. 의학 지식이 없는 50~60대 부모님께 말로 설명하듯 씁니다.
- 읽는 사람이 모를 수 있는 전문용어는 일상어로 바꿉니다. 모를지 확신이 없으면 바꿉니다.
  단, 아래 [그대로 써도 되는 말]처럼 일반 신문에 나오는 말은 바꾸지 않습니다.
- 문장은 짧게 씁니다. 한 문장에는 한 가지 내용만 담습니다.
- 어조는 일반 신문 기사처럼 차분하게 씁니다. "죽을 위험" 대신 "사망 위험"처럼 거칠지 않은 말을 씁니다.

[병 이름]
- 치매, 알츠하이머병에는 설명을 붙이지 않습니다 (독자가 치매 환자 보호자라 이미 압니다).
- 그 밖에 덜 알려진 병 이름(경도인지장애, 전두측두엽 치매, 원발성 진행성 실어증 등)만, 논문 하나에서 처음 나올 때
  딱 한 번 짧은 설명(15자 안팎)을 괄호로 붙입니다. 예: 경도인지장애(치매 전 단계)
- 세 칸(summary_background, summary_finding, summary_limitation)을 이어서 읽는 글로 봅니다.
  어느 칸에서든 한 번 설명을 붙였으면 뒤에서는 다시 붙이지 않습니다.

[그대로 써도 되는 말] 일반 신문에 나오는 말이라 바꾸지 않습니다.
진단, 증상, 치료, 약, 부작용, 의료진, 연구진, 임상시험, 요양시설, 요양원, 병원, 삶의 질, 우울, 불안, 스트레스,
설문, 면담, 사망, 비영리단체, 성소수자, 경도인지장애, 보호자, 돌봄, 인공지능, 앱

[바꿔 쓸 말] 설명을 붙이지 말고 일상어로 바꿉니다.
인지 저하 → 기억력과 판단력이 떨어지는 것 / 비공식 돌봄 → 가족이 직접 하는 돌봄 / 일차의료 → 동네 병원 /
정신행동 증상 → 감정과 행동의 변화 / 심리사회적 건강 → 마음 건강과 사회생활 /
초조 → 안절부절못하고 흥분하는 증상 / 망상 → 사실이 아닌 것을 믿는 증상 / 무관심·의욕 저하 → 무기력 /
유전형 → 타고난 유전자 차이 / 위약 → 효과 없는 가짜 약 / 항정신병 약물 → 정신과에서 쓰는 진정 효과가 있는 약 /
횡단면 → 한 시점에 조사 / 후향적 → 이미 있던 기록을 분석 / 무작위 대조시험 → 무작위로 나눠 비교한 임상시험 /
문헌고찰 → 기존 연구를 모아 정리한 연구 / 인지 기능 → 기억력과 판단력 / 낙상 → 넘어짐 /
약물 상호작용·상호작용 → 다른 약과 함께 먹을 때 생기는 영향 / 비약물적 → 약을 쓰지 않는
- 이 목록은 title_ko에도 똑같이 적용합니다. 초조는 제목에서도 "안절부절못하고 흥분하는 증상"으로 쓰고,
  제목이 길어지면 "흥분 증상"처럼 줄여도 됩니다.
- 초조, 항정신병, 인지 기능, 인지 저하, 낙상, 상호작용, 위약, 망상, 일차의료라는 말은 제목과 본문 어디에도 쓰지 않습니다.
- 병의 세부 유형, 유전자 이름, 뇌 부위 이름은 나열하지 않습니다.
  "종류에 따라 달랐다", "뇌의 여러 부분이 관련됐다"처럼 묶어서 씁니다.
- 약이나 뇌의 작용 원리(효소, 수용체 이름 등)는 쓰지 않습니다. 필요하면 "뇌의 신호 전달에 작용하는 약" 정도로만 씁니다.

[뜻을 바꾸지 말아야 할 것] 쉬운 말로 바꿀 때 특히 주의합니다.
- 연구 방식: 임상시험인지, 설문인지, 면담인지, 기록 분석인지, 기존 연구를 정리한 글인지.
- 대상이 누구이고 어디서 지내는지 (예: "기억력 클리닉에서 모집한, 집이나 요양시설에서 지내는 환자"를
  "병원에서 생활하는 환자"로 바꾸지 않습니다).
- 비교 대상(가짜 약인지 다른 약인지), 늘었는지 줄었는지, "관련이 있다"인지 "원인이다"인지.

[영어와 이름]
- 척도, 설문, 연구 이름, 기관, 통계 용어, 연구 방법 이름은 영어도 약자도 쓰지 않고 쉬운 말로 풀어 씁니다
  (예: "보호자 부담을 재는 설문", "미국의 한 의료기관").
- 약 이름만 본문에 처음 나올 때 한 번, 한글 이름 뒤 괄호에 영어를 씁니다 (예: 아우벨리티(Auvelity)).
  성분명은 한글로만 씁니다. 그 밖에는 본문에 영어 단어를 쓰지 않습니다.
- title_ko도 같은 기준으로 쉽게 씁니다. 병 이름 외의 전문용어와 영어는 쓰지 않습니다. 약 이름도 한글로만 씁니다.
  title_ko에는 괄호 설명을 붙이지 않습니다. 30자 안팎으로 짧게, 무엇에 관한 연구인지만 알 수 있게 씁니다.
  예: "히스패닉·라티노의 인지 저하와 관련된 새로운 유전적 특징" → "중남미계 사람들의 기억력 저하와 관련된 유전자 특징"

[숫자]
- 통계 수치는 쓰지 않습니다: 오즈비, 신뢰구간, p값, 베타, 표준편차, 효과크기 등.
  대신 "더 높았다", "뚜렷한 차이는 없었다", "조금 나아졌다"처럼 말로 설명합니다.
- 이해하기 쉬운 숫자만 꼭 필요할 때 씁니다: 참여한 사람 수, 기간, 간단한 비율(예: "10명 중 7명").
  한 칸에 숫자는 2~3개 이내로 씁니다. 쓰는 숫자는 초록에 적힌 그대로 옮깁니다.
- 숫자를 말로 바꿀 때 초록의 뜻을 바꾸지 않습니다 (예: 오즈비를 "몇 배"로 바꾸지 않습니다).

[쉬운 정도 예시] 아래 글의 내용은 따라 쓰지 말고, 쉬운 정도만 참고합니다.
- 어려운 예: "원발성 진행성 실어증의 여러 유형에서 감정 변화, 행동 변화, 무관심, 망상과 같은 증상이 나타났습니다. \
행동 문제는 의미형에서 가장 두드러졌습니다."
- 쉬운 예: "말하고 알아듣는 능력이 점점 떨어지는 치매에서도 감정 기복, 행동 변화, 무기력, 사실이 아닌 것을 믿는 \
증상이 흔하게 나타났습니다. 어떤 증상이 두드러지는지는 이 치매의 세부 종류에 따라 달랐습니다. 이런 증상은 환자의 \
일상생활을 더 어렵게 하고 가족의 돌봄 부담도 키웠습니다."
"""

SYSTEM_PROMPT = f"""\
당신은 치매 연구 논문을 가족 보호자에게 쉬운 말로 전하는 편집자입니다.
주어진 논문의 제목과 초록만 보고 아래 네 칸을 한국어로 작성합니다.

[내용]
- 반드시 제목과 초록에 있는 내용만 씁니다. 초록에 없는 사실이나 외부 지식은 쓰지 않습니다.
- 치료를 권하거나 지시하는 표현("복용하세요", "~해야 합니다" 등)은 쓰지 않습니다.
- 정해진 글자 수는 없습니다. 분량은 내용에 맞게 쓰고, 칸을 채우려고 내용을 늘리지 않습니다.

{EASY_WORD_RULES}
[칸별 내용]
- title_ko: 원제의 뜻을 살린 자연스러운 한국어 제목 한 줄 (30자 안팎, 괄호 설명 없음).
- summary_background (① 연구 배경): 왜 이 연구를 했는지, 기존에 알려진 것이나 부족했던 것.
  배경 설명이 아예 없을 때만 "{NO_BACKGROUND}."라고 씁니다.
  결과가 보호자에게 주는 의미나 시사점은 쓰지 않습니다.
- summary_finding (② 새로 밝혀진 것): 무엇을 누구에게 어떻게 연구했고 무엇을 발견했는지.
  결과를 모두 나열하지 말고 보호자에게 중요한 핵심 2~3가지 위주로 씁니다. ①의 배경을 반복하지 않습니다.
  이 칸에는 결과만 씁니다. "앞으로 더 연구가 필요하다", "효과를 확인하려면 비교 연구가 필요하다" 같은 내용은
  ③ 한계에 씁니다.
- summary_limitation (③ 한계): 초록에 나온 사실(사람 수, 한 기관, 짧은 기간, 비교 집단 없음 등)을 바탕으로
  "그래서 결과를 볼 때 무엇을 주의해야 하는지"를 쉬운 말로 씁니다.
  쓸 내용이 정말 없으면 "{NO_LIMITATION}" 한 문장만 씁니다.
- 어느 칸에도 "초록"이라는 말을 쓰지 않습니다 (독자는 초록이 무엇인지 모릅니다).
- drugs: 요약 본문에 쓴 약 이름마다 ko(본문에 쓴 한글 이름 그대로)와 en(초록에 적힌 영어 이름 그대로)을 적습니다.
  본문은 ① → ② → ③ 순서로 이어 읽는 글입니다. 그 약 이름이 가장 먼저 나오는 곳에 반드시 영어 괄호를 붙입니다
  (예: 아우벨리티(Auvelity)). 상품명 뒤에 덧붙인 성분명은 drugs에 넣지 않습니다. 본문에 약 이름이 없으면 빈 목록입니다.
"""

POLISH_PROMPT = f"""\
당신은 치매 연구 요약을 가족 보호자가 읽기 쉽게 다듬는 교정자입니다.
주어진 요약 네 칸(title_ko, summary_background, summary_finding, summary_limitation)에서
의학 지식이 없는 사람이 모를 단어나 표현을 찾아 쉬운 말로 바꿉니다.

[지켜야 할 것]
- 내용, 수치, 비교 대상은 바꾸지 않습니다. 새 내용을 더하거나 있던 내용을 빼지 않습니다.
- 어려운 단어·표현만 바꿉니다. 이미 쉬운 문장은 그대로 둡니다.
- 치료를 권하거나 지시하는 표현은 쓰지 않습니다. "초록"이라는 말도 쓰지 않습니다.
- 네 칸을 모두 돌려줍니다. 바꿀 것이 없는 칸은 받은 그대로 돌려줍니다.
- changes에는 바꾼 단어·표현을 하나씩 적습니다. before는 받은 글에 있던 표현 그대로,
  after는 돌려주는 글에 쓴 표현 그대로 적습니다. 바꾼 것이 없으면 빈 목록입니다.

{EASY_WORD_RULES}"""


class Summary(BaseModel):
    """LLM이 돌려줘야 하는 형식. 빈 칸이나 모르는 칸이 있으면 검증에서 걸린다."""

    model_config = ConfigDict(extra="forbid")

    title_ko: str = Field(min_length=1)
    summary_background: str = Field(min_length=1)
    summary_finding: str = Field(min_length=1)
    summary_limitation: str = Field(min_length=1)


class Drug(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ko: str
    en: str


class Draft(Summary):
    """처음 쓰기의 응답 형식: 네 칸 + 본문에 쓴 약 이름 목록 (영어 괄호 검사에 쓴다, 저장하지 않는다)."""

    drugs: list[Drug] = []


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: str
    after: str


class Polished(Summary):
    """두 번째 다듬기의 응답 형식: 다듬은 네 칸 + 바꾼 표현 목록."""

    changes: list[Change]


def _response_format(name: str, extra: dict | None = None) -> dict:
    properties = {field: {"type": "string"} for field in Summary.model_fields} | (extra or {})
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


def _pairs(first: str, second: str) -> dict:
    """문자열 두 개짜리 객체의 목록 (drugs, changes)."""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {first: {"type": "string"}, second: {"type": "string"}},
            "required": [first, second],
            "additionalProperties": False,
        },
    }


SUMMARY_FORMAT = _response_format("paper_summary", {"drugs": _pairs("ko", "en")})
POLISH_FORMAT = _response_format("paper_polish", {"changes": _pairs("before", "after")})

# ---- 자동 검사 (생성 직후, 저장 전) ----
# 통계 표기: p =, p<, CI, OR =, β, SD. 앞뒤가 영문자가 아닐 때만 잡는다 ("SD"는 잡고 "SDGs"는 안 잡는다).
_STAT = re.compile(r"(?<![A-Za-z])(?:[pP]\s*[=<>≤≥]|CI(?![A-Za-z])|OR\s*=|SD(?![A-Za-z]))|β")
# 본문에서 허용하는 영어: 한글 바로 뒤 괄호 안의 영어 — 아우벨리티(Auvelity).
# ponytail: 괄호 안이 약 이름인지는 구분하지 못한다 (인공지능(AI), 척도 이름(영어)도 통과한다).
# 약 이름만 허용해야 하면 초록에서 뽑은 약 이름 목록과 대조하는 식으로 바꾼다.
_ALLOWED_ENGLISH = re.compile(r"(?<=[가-힣])\([A-Za-z][A-Za-z0-9 .\-]*\)")
_ENGLISH_WORD = re.compile(r"[A-Za-z]{2,}")
_BROKEN_WORD = re.compile(r"[가-힣]+[A-Za-z]+[가-힣]+")  # 한글 사이에 영문자가 낀 단어
# 제목과 본문 어디에도 나오면 안 되는 말 (프롬프트의 [바꿔 쓸 말] 왼쪽 단어 + "초록"). 띄어쓰기는 무시하고 찾는다.
# ponytail: 단순 포함 검사라 "초조해하다", "사회적 상호작용", "초록색"처럼 일상어로 쓴 경우도 걸린다.
BANNED_WORDS = ["초조", "항정신병", "인지 기능", "인지 저하", "낙상", "상호작용", "위약", "망상", "일차의료", "초록"]
_DRUG_ENGLISH = re.compile(r"\s?\(([^)]*)\)")  # 약 이름 바로 뒤의 괄호


def check(summary: Summary, drugs: list["Drug"] | tuple = (), source: str = "") -> list[str]:
    """쉬운 말 규칙을 어긴 곳을 찾는다. 문제가 없으면 빈 목록.

    drugs는 LLM이 적어 준 '본문에 쓴 약 이름' 목록, source는 논문 제목 + 초록이다.
    약 이름이 본문(① → ② → ③ 순)에 처음 나오는 곳에 영어 괄호가 없으면 걸린다.
    """
    problems = []
    for name, text in summary.model_dump().items():
        squeezed = text.replace(" ", "")
        if found := [word for word in BANNED_WORDS if word.replace(" ", "") in squeezed]:
            problems.append(f"{name}: 금지 단어 {found}")
        if found := _STAT.findall(text):
            problems.append(f"{name}: 통계 표기 {sorted(set(found))}")
        # 제목은 괄호 안 영어도 허용하지 않는다.
        plain = text if name == "title_ko" else _ALLOWED_ENGLISH.sub("", text)
        if found := _BROKEN_WORD.findall(plain):
            problems.append(f"{name}: 깨진 단어 {sorted(set(found))}")
        elif found := _ENGLISH_WORD.findall(plain):
            problems.append(f"{name}: 영어 단어 {sorted(set(found))}")

    body = "\n".join(getattr(summary, name) for name, _ in SECTIONS)
    for drug in drugs:
        # ponytail: 약 이름은 LLM이 drugs에 적어 준 것만 검사한다 (목록에서 빠뜨린 약은 못 잡는다).
        # 영어 이름이 실제로 제목·초록에 있는 것만 믿는다 — 없는 이름을 지어낸 경우는 검사하지 않는다.
        if not drug.ko or not drug.en or drug.en.lower() not in source.lower() or drug.ko not in body:
            continue
        after_name = body[body.index(drug.ko) + len(drug.ko):]
        match = _DRUG_ENGLISH.match(after_name)
        if not match or drug.en.lower() not in match.group(1).lower():
            problems.append(f"약 이름 영어 괄호 빠짐: 본문에 처음 나오는 '{drug.ko}' 뒤에 ({drug.en})가 없음")
    return problems


@dataclass
class Report:
    """배치 한 번의 결과. 번호는 대상 목록에서의 순번(1부터)이다."""

    # (번호, id, 원제, 초록, 요약, 다듬기에서 바뀐 표현 [(전, 후, 실제 글에서 확인됐는지)] — 다듬기를 안 했으면 None)
    done: list[tuple[int, UUID, str, str, Summary, list[tuple[str, str, bool]] | None]] = field(
        default_factory=list
    )
    no_abstract: int = 0
    failed: list[tuple[int, UUID, str]] = field(default_factory=list)  # (번호, id, 이유)
    # 자동 검사에 걸려 다시 생성한 논문: (번호, 처음 걸린 이유, 다시 만든 뒤 통과했는지)
    regenerated: list[tuple[int, list[str], bool]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    max_completion_tokens: int = 0  # 요청 하나가 쓴 출력 토큰의 최댓값 (MAX_TOKENS 여유 확인용)


def ask(client, model: str, system: str, user: str, response_format: dict, schema, report: Report):
    """LLM을 한 번 부르고 schema로 검증한다. API 오류나 형식이 틀린 응답이면 최대 MAX_RETRIES번 다시 시도한다."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_format,
                max_completion_tokens=MAX_TOKENS,
                timeout=TIMEOUT_SECONDS,
            )
            if response.usage:  # 실패한 시도도 토큰은 쓰였으므로 센다
                report.prompt_tokens += response.usage.prompt_tokens
                report.completion_tokens += response.usage.completion_tokens
                report.max_completion_tokens = max(
                    report.max_completion_tokens, response.usage.completion_tokens
                )
            if response.choices[0].finish_reason == "length":
                raise ValueError(f"응답이 토큰 한도({MAX_TOKENS})에서 잘렸습니다. MAX_TOKENS를 올려 주세요.")
            return schema.model_validate_json(response.choices[0].message.content or "")
        except Exception:
            if attempt == MAX_RETRIES:
                raise


def summarize(
    client, model: str, title: str, abstract: str, report: Report,
    problems: list[str] | None = None, polish: bool = False,
) -> tuple[Summary, list[tuple[str, str, bool]] | None, list[Drug]]:
    """논문 한 편을 요약한다. polish=True면 쉬운 말로 한 번 더 다듬는다 (LLM을 한 번 더 부른다).

    problems를 주면 (자동 검사에 걸려 다시 만드는 경우) 무엇을 고쳐야 하는지 함께 알려 준다.
    돌려주는 것은 (요약, 다듬기에서 바뀐 표현 목록 — 다듬기를 안 했으면 None, 본문에 쓴 약 이름 목록)이다.
    """
    content = f"제목: {title}\n\n초록:\n{abstract}"
    if problems:
        content += (
            "\n\n앞서 쓴 요약이 아래 문제로 자동 검사에 걸렸습니다. 금지 단어는 [바꿔 쓸 말]대로 쉬운 말로 바꾸고, "
            "통계 표기와 영어 단어는 풀어 쓰고, 약 이름이 처음 나오는 곳에는 영어 괄호를 붙여 다시 써 주세요.\n- "
            + "\n- ".join(problems)
        )
    draft = ask(client, model, SYSTEM_PROMPT, content, SUMMARY_FORMAT, Draft, report)
    drugs, draft = draft.drugs, Summary(**draft.model_dump(exclude={"drugs"}))
    if not polish:
        return draft, None, drugs

    # ponytail: 다듬기에는 초록을 주지 않는다 (토큰 절약). 다듬다가 뜻이 바뀌는 일이 잦으면 초록도 같이 준다.
    polished = ask(
        client, model, POLISH_PROMPT, draft.model_dump_json(indent=2), POLISH_FORMAT, Polished, report
    )
    summary = Summary(**polished.model_dump(exclude={"changes"}))
    # 바뀐 표현 목록은 LLM이 스스로 적은 것이라, 실제로 그렇게 바뀌었는지 글에서 확인해 표시한다.
    before_text, after_text = " ".join(draft.model_dump().values()), " ".join(summary.model_dump().values())
    changes = [
        (c.before, c.after, c.before in before_text and c.after in after_text)
        for c in polished.changes
        if c.before != c.after
    ]
    return summary, changes, drugs


def run(
    papers: list[tuple[UUID, str, str | None]], client, model: str,
    dry_run: bool = False, polish: bool = False,
) -> Report:
    """(id, 제목, 초록) 목록을 요약해서 저장한다. dry_run이면 DB에 쓰지 않는다.

    자동 검사(check)는 저장할 최종 글(polish=True면 다듬은 글)에 돌린다. 걸린 논문은 한 번만 다시 만들고,
    또 걸리면 저장하지 않고 실패로 남긴다.
    """
    report = Report()
    for number, (paper_id, title, abstract) in enumerate(papers, 1):
        if not (abstract or "").strip():
            report.no_abstract += 1
            continue
        try:
            source = f"{title}\n{abstract}"
            summary, changes, drugs = summarize(client, model, title, abstract, report, polish=polish)
            if first_problems := check(summary, drugs, source):
                summary, changes, drugs = summarize(
                    client, model, title, abstract, report, first_problems, polish
                )
                problems = check(summary, drugs, source)
                report.regenerated.append((number, first_problems, not problems))
                if problems:
                    raise ValueError("자동 검사에 두 번 걸림 — " + "; ".join(problems))
        except Exception as exc:
            report.failed.append((number, paper_id, f"{type(exc).__name__}: {exc}"[:300]))
            print(f"[{number}/{len(papers)}] 실패 {paper_id}")
            continue
        if not dry_run:
            functions.save_summary(AnalysisIn(paper_id=paper_id, model_name=model, **summary.model_dump()))
        report.done.append((number, paper_id, title, abstract, summary, changes))
        print(f"[{number}/{len(papers)}] {summary.title_ko}")
    return report


# 결과 파일에 보이는 칸 순서와 이름.
SECTIONS = [
    ("summary_background", "① 연구 배경"),
    ("summary_finding", "② 새로 밝혀진 것"),
    ("summary_limitation", "③ 한계"),
]


# "치매(…)", "알츠하이머병(…)"처럼 누구나 아는 병 이름에 괄호 설명이 붙은 곳. 결과 파일 머리말에서 센다.
# 앞에 한글이 바로 붙은 경우는 뺀다. 띄어 쓴 세부 병명("전두측두엽 치매(…)")은 아래 목록에 있는 것만 뺀다.
# ponytail: 목록에 없는 세부 병명은 같이 세어진다. 머리말에 걸린 곳을 그대로 보여 주므로 눈으로 가려낸다.
_COMMON_DISEASE_EXPLAINED = re.compile(
    r"(?<![가-힣])(?<!전두측두엽 )(?<!전측두엽 )(?<!루이소체 )(?<!혈관성 )(?<!초로기 )(?<!조발성 )"
    r"(?:알츠하이머병?\s?치매|알츠하이머병|치매)\s?\([^)]*\)"
)


def token_line(report: Report) -> str:
    return (
        f"토큰 사용량: 입력 {report.prompt_tokens:,} + 출력 {report.completion_tokens:,} "
        f"= {report.prompt_tokens + report.completion_tokens:,} "
        f"(요청당 최대 출력 {report.max_completion_tokens:,} / 한도 {MAX_TOKENS:,})"
    )


def to_markdown(report: Report, model: str) -> str:
    """사람이 읽고 톤·분량을 확인할 수 있는 결과 파일 내용. 맨 위에 통계와 자동 검사 결과를 둔다."""
    summaries = [done[4] for done in report.done]
    parts = [f"# 논문 요약 시험 결과 ({len(summaries)}편, 모델: {model})\n"]
    if summaries:
        parts.append("| 칸 | 최소 | 중앙값 | 최대 |\n|---|---|---|---|")
        for name, label in [("title_ko", "title_ko"), *SECTIONS]:
            sizes = [len(getattr(s, name)) for s in summaries]
            parts.append(f"| {label} | {min(sizes)}자 | {statistics.median(sizes):g}자 | {max(sizes)}자 |")
        no_background = sum(NO_BACKGROUND in s.summary_background for s in summaries)
        no_limitation = sum(NO_LIMITATION in s.summary_limitation for s in summaries)
        parts.append(f'\n- "{NO_BACKGROUND}"가 들어간 칸: {no_background}개 / {len(summaries)}편')
        parts.append(f'- "{NO_LIMITATION}"가 들어간 칸: {no_limitation}개 / {len(summaries)}편')
        explained = [
            (number, snippet)
            for number, *_, s, _ in report.done
            for snippet in _COMMON_DISEASE_EXPLAINED.findall(" ".join(s.model_dump().values()))
        ]
        parts.append(f'- "치매"·"알츠하이머병" 뒤에 괄호 설명이 붙은 곳: {len(explained)}개')
        parts.extend(f"  - {number}번: {snippet}" for number, snippet in explained)
        change_counts = [len(done[5]) for done in report.done if done[5] is not None]
        if change_counts:
            parts.append(
                f"- 두 번째 다듬기에서 바뀐 표현: 모두 {sum(change_counts)}개 "
                f"(바뀐 논문 {sum(1 for n in change_counts if n)}편 / {len(change_counts)}편)"
            )
        else:
            parts.append("- 두 번째 다듬기: 하지 않음 (--polish 없음)")
    parts.append(f"- {token_line(report)}")
    parts.append(f"- 자동 검사에 걸려 다시 생성한 논문: {len(report.regenerated)}편")
    for number, problems, passed in report.regenerated:
        result = "통과" if passed else "다시 걸려 저장하지 않음"
        parts.append(f"  - {number}번: 재생성 1회 → {result}. 처음 걸린 이유: {'; '.join(problems)}")
    for label in ("금지 단어", "약 이름 영어 괄호"):
        hits = [(n, passed) for n, problems, passed in report.regenerated if any(label in p for p in problems)]
        parts.append(
            f"- {label} 검사: 걸린 논문 {len(hits)}편 {[n for n, _ in hits]}, 재생성 {len(hits)}회, "
            f"재생성 뒤에도 걸려 최종 실패 {sum(not passed for _, passed in hits)}편"
        )
    parts.append(f"- 실패: {len(report.failed)}편")
    for number, paper_id, reason in report.failed:
        parts.append(f"  - {number}번 (`{paper_id}`): {reason}")
    parts.append("")
    for number, paper_id, title, abstract, s, changes in report.done:
        head = "\n".join(f"> {line}" for line in textwrap.wrap(abstract, 100)[:3])
        parts.append(
            f"## {number}. {s.title_ko}\n\n"
            f"- 원제: {title}\n- paper_id: `{paper_id}`\n\n"
            f"초록 앞부분:\n\n{head}\n"
        )
        for name, label in SECTIONS:
            text = getattr(s, name)
            parts.append(f"**{label}** ({len(text)}자)\n\n{text}\n")
        if changes is None:  # 다듬기를 하지 않은 실행
            continue
        parts.append(f"**다듬기에서 바뀐 표현** ({len(changes)}개)\n")
        # (글에서 확인 안 됨) = LLM이 바꿨다고 적었지만 처음 글이나 다듬은 글에서 그 표현을 찾지 못한 경우.
        parts.extend(
            f"- {before} → {after}" + ("" if verified else " (글에서 확인 안 됨)")
            for before, after, verified in changes
        )
        parts.append("" if changes else "- 없음\n")
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="논문 3칸 요약 + 한국어 제목 → paper_analysis 저장")
    ap.add_argument("--limit", type=int, default=20, help="최대 처리 편수 (기본 20)")
    ap.add_argument("--paper-ids", nargs="+", type=UUID, help="이 papers.id들만 처리 (요약이 있어도 다시 만든다)")
    ap.add_argument("--dry-run", action="store_true", help="DB에 저장하지 않고 결과만 --out 파일로")
    ap.add_argument("--out", type=Path, help="결과를 적을 마크다운 파일 경로")
    ap.add_argument("--polish", action="store_true",
                    help="쉬운 말로 한 번 더 다듬는다 (LLM을 한 번 더 불러 토큰이 약 3배). 기본은 끔")
    args = ap.parse_args()

    if args.dry_run and not args.out:
        ap.error("--dry-run은 결과를 볼 파일이 필요합니다. --out 경로를 함께 주세요.")
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY가 비어 있습니다. backend/.env에 키를 넣어 주세요.")
    model = settings.openai_summary_model
    if not model:
        raise SystemExit(
            "OPENAI_SUMMARY_MODEL이 비어 있습니다. backend/.env에 요약에 쓸 OpenAI 모델 이름을 "
            "넣어 주세요 (예: OPENAI_SUMMARY_MODEL=<모델 이름>). 기본 모델은 일부러 두지 않았습니다."
        )

    if args.paper_ids:
        papers = [(p.paper_id, p.title, p.abstract) for p in functions.get_papers_by_ids(args.paper_ids)]
    else:
        # ponytail: 초록 없는 논문은 요약이 안 생겨 매번 다시 나온다. limit을 그런 논문이 다 채우면
        # get_new_papers에 '초록 있는 것만' 조건을 추가한다 (팀 기준 1,000편은 전부 초록이 있다).
        papers = [(p.id, p.title, p.abstract) for p in functions.get_new_papers(limit=args.limit)]
    print(f"대상 {len(papers)}편, 모델 {model}" + (" (dry-run: DB에 저장하지 않음)" if args.dry_run else ""))

    from openai import OpenAI

    # 재시도는 ask()가 직접 센다 (SDK 자체 재시도와 겹치지 않게 0).
    report = run(
        papers, OpenAI(api_key=settings.openai_api_key, max_retries=0), model, args.dry_run, args.polish
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(to_markdown(report, model), encoding="utf-8")
        print(f"결과 파일: {args.out.resolve()}")
    print(
        f"\n완료 {len(report.done)}편{' (저장 안 함)' if args.dry_run else ' 저장'}, "
        f"초록 없음 {report.no_abstract}편, 실패 {len(report.failed)}편"
    )
    print(f"자동 검사로 다시 생성 {len(report.regenerated)}편: {[n for n, *_ in report.regenerated]}")
    for number, paper_id, reason in report.failed:
        print(f"  실패 {number}번 {paper_id}: {reason}")
    print(token_line(report))


if __name__ == "__main__":
    main()
