"""사용자·환자 도메인 데이터 접근 함수의 입출력 스키마 (팀 공용 계약, 김한슬 담당).

논문 도메인의 `app/schemas.py`와 같은 역할을 사용자·환자 도메인에서 맡는다.
테이블 정의는 `app/db/migrations/001_init.sql`(users, patient_profiles, caregiver_profiles)
+ `004_patient_management.sql`(phone_number, clinical_assessments, safety_events, medications,
medical_visits) + `005_medical_visits_detail.sql`(진료 항목 컬럼)이 정본이고,
이 파일은 그 스키마를 파이썬 쪽에서 드러내는 계약이다.
`app/db/user_functions.py`는 이 모델로 입력을 검증하고, 이 모델로 결과를 돌려준다.
스키마가 바뀌면 반드시 AGENTS.md의 "데이터 접근 계약" 섹션도 갱신한다.

모델 이름 규칙:
- XxxIn     : 저장(create / upsert) 입력. id / created_at / updated_at은 DB가 채우므로 없다.
- XxxUpdate : 수정 입력. 모든 필드가 선택이고 **실제로 보낸 필드만** 바뀐다.
              보내지 않음 = 기존 값 유지 / null로 보냄 = 값 삭제 (NOT NULL 컬럼은 null을 거부한다).
- XxxOut    : 저장·조회 결과 (DB 행).

입력 모델은 모르는 필드를 거부한다(extra="forbid"). 오타('phone' ↔ 'phone_number')나
DB가 채워야 할 값('id', 'created_at')이 조용히 무시되거나 저장되지 않게 하기 위함이다.

검증을 나누는 기준:
- 이 파일: 입력 하나만 보고 판단할 수 있는 형식 검사 (형식, 범위, 같은 입력 안의 필드 조합).
- user_functions.py: DB의 현재 값이 있어야 판단할 수 있는 검사
  (없는 환자·사용자, 이메일 중복, 수정 후 값 조합 — 예: 점수만 고쳤을 때 기존 검사명 기준 범위).
"""
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

# ------------------------------------------------------------
# 공유 상수 — 검증 규칙의 정본. 바꾸면 앱 입력 화면·AI 해석도 같이 바뀌므로 팀에 공지한다.
# ------------------------------------------------------------

# 휴대폰 번호: 010 + 8자리가 대부분이고, 011/016/017/018/019는 7~8자리가 남아 있다.
# [0-9]로 ASCII 숫자만 받는다 (\d는 전각·아랍 숫자도 통과시켜 같은 번호가 중복 저장될 수 있다).
PHONE_PATTERN = re.compile(r"^01[016789][0-9]{7,8}$")
# 이메일: 형식만 간단히 본다 (EmailStr은 email-validator 패키지가 필요해 쓰지 않았다).
# 출력 가능한 ASCII만 받는다 — 눈에 안 보이는 문자를 섞어 같은 주소를 중복 가입하는 일을 막는다.
EMAIL_PATTERN = re.compile(r"^[!-?A-~]+@[!-?A-~]+\.[!-?A-~]+$")
# 이메일 최대 길이 (RFC 5321 실무 한도). 더 길면 UNIQUE 인덱스 한도에 걸려 DB 에러가 난다.
EMAIL_MAX_LENGTH = 254

# "미래 날짜" 판정 기준 시간대 = 한국 표준시. 서버(docker)가 UTC여도 한국 날짜로 판단해야
# 한국 시간 00~09시에 "오늘" 기록이 미래로 거부되지 않는다. 한국은 서머타임이 없어 고정 +9시간이고,
# zoneinfo("Asia/Seoul")는 Windows에서 tzdata 패키지가 필요해 고정 오프셋을 쓴다.
APP_TIMEZONE = timezone(timedelta(hours=9), "KST")

# 진단 단계. 앱 대상이 치매·경도인지장애라 001 주석의 경도/중등도/중증에 경도인지장애를 더했다.
DEMENTIA_STAGES = ("경도인지장애", "경도", "중등도", "중증")

# 간병인 자가점검 점수 범위 (양 끝 포함).
#   burden_score — Zarit 부담척도. 판본(4·12·22문항)마다 만점이 달라 가장 넓은 22문항 기준 0~88로 받는다.
#   mood_score   — PHQ-9 우울 셀프체크 0~27.
BURDEN_SCORE_RANGE = (0, 88)
MOOD_SCORE_RANGE = (0, 27)

# clinical_assessments.score는 NUMERIC(5, 2) — 소수 둘째 자리까지, 절댓값 1000 미만까지 저장된다.
SCORE_QUANTUM = Decimal("0.01")
SCORE_LIMIT = Decimal("1000")

# 국내에서 쓰는 치매 관련 검사와 점수 규칙.
#   min / max  : 점수 범위 (양 끝 포함)
#   step       : 점수 단위. 1이면 정수만, 0.5면 0.5 단위만. None이면 소수 둘째 자리까지 자유
#   allowed    : 정해진 값만 허용하는 검사 (CDR). 있으면 min/max/step 대신 쓴다
#   higher_is_worse : 점수가 높을수록 나쁜 상태인지 (그래프·AI 해석용)
# ★ 새 검사를 받으려면 여기에 한 줄 추가한다. 목록에 없는 검사명은 저장을 거부한다
#   (검사명이 제각각 저장되면 같은 검사끼리 추이를 볼 수 없기 때문이다).
ASSESSMENT_TYPES: dict[str, dict[str, Any]] = {
    # --- 인지 선별·평가 (높을수록 좋음) ---
    "K-MMSE": {"name": "한국판 간이정신상태검사", "min": 0, "max": 30, "step": 1, "higher_is_worse": False},
    "MMSE-DS": {"name": "치매선별용 한국어판 간이정신상태검사", "min": 0, "max": 30, "step": 1, "higher_is_worse": False},
    "CIST": {"name": "인지선별검사 (국가치매검진)", "min": 0, "max": 30, "step": 1, "higher_is_worse": False},
    "K-MoCA": {"name": "한국판 몬트리올 인지평가", "min": 0, "max": 30, "step": 1, "higher_is_worse": False},
    # --- 보호자 설문형 선별 (높을수록 나쁨) ---
    "KDSQ-C": {"name": "한국형 치매 선별 설문 (15문항 × 0~2점)", "min": 0, "max": 30, "step": 1, "higher_is_worse": True},
    "K-QDRS": {"name": "한국판 신속치매평가척도 (10문항, 0.5 단위)", "min": 0, "max": 30, "step": 0.5, "higher_is_worse": True},
    # --- 중증도 단계 (높을수록 나쁨) ---
    "CDR": {"name": "임상치매척도 (전반 점수)", "allowed": [0, 0.5, 1, 2, 3], "higher_is_worse": True},
    "CDR-SB": {"name": "임상치매척도 6개 영역 합계 (Sum of Boxes)", "min": 0, "max": 18, "step": 0.5, "higher_is_worse": True},
    "GDS": {"name": "전반적 퇴화 척도 (Global Deterioration Scale, 1~7단계) — 노인우울척도 아님",
            "min": 1, "max": 7, "step": 1, "higher_is_worse": True},
    # --- 일상생활 기능 (높을수록 나쁨) ---
    "K-IADL": {"name": "한국형 도구적 일상생활활동 (평균 점수)", "min": 0, "max": 3, "step": None, "higher_is_worse": True},
    # --- 행동·심리 증상 / 우울 (높을수록 나쁨) ---
    "K-NPI": {"name": "한국판 신경정신행동검사 (12개 영역 총점)", "min": 0, "max": 144, "step": 1, "higher_is_worse": True},
    "SGDS-K": {"name": "한국판 단축형 노인우울척도", "min": 0, "max": 15, "step": 1, "higher_is_worse": True},
}

# 검사명 별칭 → 표준 이름. 비교할 때는 대문자로 바꾸고 공백·'-'·'_'를 지운 키를 쓴다
# ('k-mmse', 'K MMSE', 'KMMSE' 모두 'K-MMSE'로 저장된다).
ASSESSMENT_ALIASES = {
    "MMSEK": "K-MMSE",
    "MOCAK": "K-MoCA",
    "CDRSOB": "CDR-SB",
    "KSGDS": "SGDS-K",
    "NPI": "K-NPI",
    "QDRS": "K-QDRS",
}

# 프로필 묶음 조회(get_user_profile_context)에 담는 기록의 범위.
# AI 피드·챗봇이 "지금 상태"로 읽는 값이라, 바꾸면 AI 쪽 결과도 달라지므로 팀에 공지한다.
#   PROFILE_SAFETY_DAYS   — 오늘 포함 최근 며칠의 안전·행동 기록을 담을지 (이벤트가 있는 날만)
#   PROFILE_RECENT_VISITS — 다녀온 진료를 최근 몇 건까지 담을지
PROFILE_SAFETY_DAYS = 30
PROFILE_RECENT_VISITS = 3


# ------------------------------------------------------------
# 값 정리·검사 함수 — 모델의 validator가 쓴다.
# 밑줄 없는 함수 중 check_* / normalize_assessment_type / parse_optional_date는
# user_functions.py도 쓴다 (수정 후 조합 검사, 조회 필터). 나머지는 모델 validator 전용이다.
# ------------------------------------------------------------


def today() -> date:
    """한국 시간 기준 오늘 날짜. "미래 날짜" 판정은 모두 이 값을 기준으로 한다."""
    return datetime.now(APP_TIMEZONE).date()


def _check_characters(text: str, field: str) -> None:
    """DB에 저장할 수 없는 문자를 거부한다.

    - NUL(\\x00): PostgreSQL TEXT에 저장되지 않아 DB 에러가 난다 (JSON의 "\\u0000"으로 들어올 수 있다).
    - 짝이 없는 서로게이트: UTF-8로 인코딩되지 않아 DB 에러가 난다.
    """
    if "\x00" in text:
        raise ValueError(f"{field}에 사용할 수 없는 문자(NUL)가 있습니다.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{field}에 사용할 수 없는 문자가 있습니다.") from None


def _is_blank(text: str) -> bool:
    """공백·눈에 안 보이는 서식 문자(제로폭 공백 등)만 있으면 빈 값으로 본다."""
    return all(ch.isspace() or unicodedata.category(ch) == "Cf" for ch in text)


def _required_text(value: Any, field: str) -> str:
    """필수 텍스트 — 앞뒤 공백을 지우고, 비어 있거나 null이면 거부한다 (NOT NULL 컬럼)."""
    text = _optional_text(value, field)
    if text is None:
        raise ValueError(f"{field}는 비울 수 없습니다.")
    return text


def _optional_text(value: Any, field: str) -> Optional[str]:
    """선택 텍스트 — 앞뒤 공백을 지우고, 비어 있으면 None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field}는 문자열이어야 합니다: {value!r}")
    _check_characters(value, field)
    if _is_blank(value):
        return None
    return value.strip()


def _text_list(value: Any, field: str) -> list[str]:
    """TEXT[] 입력 — 문자열 목록의 공백을 지우고, 빈 값·중복을 뺀다 (순서 유지). null은 []."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field}는 문자열 목록이어야 합니다: {value!r}")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field}에는 문자열만 넣을 수 있습니다: {item!r}")
        _check_characters(item, field)
        if _is_blank(item):
            continue
        text = item.strip()
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _strict_bool(value: Any, field: str) -> bool:
    """참/거짓 — JSON의 true/false만 받는다 ("true" 문자열·1/0·null은 오입력으로 보고 거부)."""
    if not isinstance(value, bool):
        raise ValueError(f"{field}는 true/false여야 합니다: {value!r}")
    return value


def _int_in_range(value: Any, field: str, bounds: tuple[int, int]) -> Optional[int]:
    """선택 정수 점수 — null은 None, 정수가 아니거나 범위를 벗어나면 거부한다."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field}는 정수여야 합니다: {value!r}")
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(f"{field} 범위는 {low}~{high}입니다: {value}")
    return value


def _normalize_phone(value: Any) -> Optional[str]:
    """휴대폰 번호를 '-' 없는 숫자만 남긴 형태로 바꾼다. 빈 값·null은 None (선택 입력).

    '010-1234-5678', '010 1234 5678', '+82 10-1234-5678', 전각 '０１０－１２３４－５６７８' 모두
    '01012345678'이 된다.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"휴대폰 번호는 문자열이어야 합니다: {value!r}")
    # 전각 숫자·하이픈(한글 입력기에서 들어올 수 있다)을 ASCII로 바꾼 뒤 정리한다.
    phone = re.sub(r"[\s\-()]", "", unicodedata.normalize("NFKC", value))
    if phone == "":
        return None
    if phone.startswith("+82"):
        phone = "0" + phone[3:]
    if not PHONE_PATTERN.match(phone):
        raise ValueError(f"휴대폰 번호 형식이 아닙니다: {value!r} (예: 01012345678)")
    return phone


def normalize_email(value: Any) -> str:
    """이메일 앞뒤 공백을 지우고 소문자로 바꾼다.

    DB의 UNIQUE는 대소문자를 구분하므로 저장할 때 소문자로 맞춰야
    'A@x.com'과 'a@x.com'이 다른 계정으로 생기지 않는다.
    (나중에 이메일로 사용자를 찾는 함수를 만들면 그 입력도 이 함수로 정리해야 한다.)
    """
    if value is None:
        raise ValueError("이메일은 비울 수 없습니다.")
    if not isinstance(value, str):
        raise ValueError(f"이메일은 문자열이어야 합니다: {value!r}")
    email = value.strip().lower()
    if len(email) > EMAIL_MAX_LENGTH:
        raise ValueError(f"이메일이 너무 깁니다 (최대 {EMAIL_MAX_LENGTH}자): {len(email)}자")
    if not EMAIL_PATTERN.match(email):
        raise ValueError(f"이메일 형식이 아닙니다: {value!r}")
    return email


_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_date(value: Any, field: str, *, allow_future: bool = False) -> date:
    """date 객체나 'YYYY-MM-DD' 문자열을 date로 바꾼다. null·시각이 붙은 값·숫자는 거부한다.

    allow_future=False(기본)면 미래 날짜(한국 시간 기준, today())를 거부한다 — 평가일·이벤트일·진단일처럼
    "이미 일어난 일"의 날짜. 병원 예약일·복용 예정일처럼 앞으로의 날짜는 True로 부른다.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        # fromisoformat은 '20261001', '2026-W40-1' 같은 형식도 받아서, 'YYYY-MM-DD'만 먼저 걸러 낸다.
        text = value.strip()
        try:
            if not _DATE_PATTERN.fullmatch(text):
                raise ValueError
            parsed = date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{field}는 'YYYY-MM-DD' 형식이어야 합니다: {value!r}") from None
    elif value is None:
        raise ValueError(f"{field}는 비울 수 없습니다.")
    else:
        raise ValueError(f"{field}는 'YYYY-MM-DD' 형식이어야 합니다: {value!r}")
    if not allow_future and parsed > today():
        raise ValueError(f"{field}는 미래 날짜일 수 없습니다: {parsed.isoformat()}")
    return parsed


def parse_optional_date(value: Any, field: str, *, allow_future: bool = False) -> Optional[date]:
    """선택 날짜 — null이면 None, 아니면 parse_date()와 같다."""
    return None if value is None else parse_date(value, field, allow_future=allow_future)


def check_date_order(start: Optional[date], end: Optional[date], start_field: str, end_field: str) -> None:
    """두 날짜가 모두 있으면 시작 ≤ 끝인지 검사한다."""
    if start is not None and end is not None and end < start:
        raise ValueError(f"{end_field}({end})가 {start_field}({start})보다 빠를 수 없습니다.")


def check_visit_state(visit_date: date, is_visited: bool) -> None:
    """아직 오지 않은 날짜의 진료를 '방문 완료'로 기록하는 것을 막는다."""
    if is_visited and visit_date > today():
        raise ValueError(f"미래 날짜({visit_date})의 진료는 방문 완료(is_visited=true)로 기록할 수 없습니다.")


def _normalize_stage(value: Any) -> Optional[str]:
    """진단 단계 — DEMENTIA_STAGES 값만 받는다. 비어 있으면 None (아직 모름)."""
    stage = _optional_text(value, "dementia_stage")
    if stage is not None and stage not in DEMENTIA_STAGES:
        raise ValueError(f"dementia_stage는 {', '.join(DEMENTIA_STAGES)} 중 하나여야 합니다: {value!r}")
    return stage


def _assessment_key(name: str) -> str:
    """검사명 비교용 키 — 대문자로 바꾸고 공백·'-'·'_'를 지운다."""
    return re.sub(r"[\s\-_]", "", name.upper())


_ASSESSMENT_LOOKUP = {
    **{_assessment_key(name): name for name in ASSESSMENT_TYPES},
    **ASSESSMENT_ALIASES,
}


def normalize_assessment_type(value: Any) -> str:
    """검사명을 표준 이름으로 바꾼다 ('k-mmse' → 'K-MMSE'). 목록에 없는 검사면 ValueError."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("검사명(assessment_type)은 비울 수 없습니다.")
    canonical = _ASSESSMENT_LOOKUP.get(_assessment_key(value))
    if canonical is None:
        raise ValueError(f"지원하지 않는 검사명입니다: {value!r} (지원: {', '.join(ASSESSMENT_TYPES)})")
    return canonical


def _format_score(value: Decimal) -> str:
    """에러 메시지용 점수 표기 (3.00 → '3', 0.50 → '0.5')."""
    return format(value.normalize(), "f")


_SCORE_TEXT_PATTERN = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?")


def parse_score(value: Any) -> Optional[Decimal]:
    """점수를 Decimal로 바꾼다 (검사 종류와 무관한 형식 검사만). null은 None (점수 미기록).

    float 대신 Decimal로 다룬다 — 부동소수 오차로 0.5 단위 검사가 틀렸다고 판정되는 일을 막는다.
    숫자 문자열("2.5")도 받는다 (입력 폼에서 문자열로 오는 경우).
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"점수는 숫자여야 합니다: {value!r}")
    # 문자열은 일반 소수 표기만 받는다 (Decimal은 '2_4', '1e2', 전각 숫자도 받아 버린다).
    if isinstance(value, str) and not _SCORE_TEXT_PATTERN.fullmatch(value.strip()):
        raise ValueError(f"점수는 숫자여야 합니다: {value!r}")
    try:
        score = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"점수는 숫자여야 합니다: {value!r}") from None
    if not score.is_finite():
        raise ValueError(f"점수는 숫자여야 합니다: {value!r}")
    # NUMERIC(5, 2)는 절댓값 1000 미만만 담는다. 아주 큰 값은 아래 quantize가
    # ValueError가 아닌 decimal.InvalidOperation을 내므로 먼저 거른다.
    if abs(score) >= SCORE_LIMIT:
        raise ValueError(f"점수가 너무 큽니다 (절댓값 {SCORE_LIMIT} 미만): {value!r}")
    if score != score.quantize(SCORE_QUANTUM):
        raise ValueError(f"점수는 소수 둘째 자리까지만 저장할 수 있습니다: {value!r}")
    return score


def check_assessment_score(assessment_type: str, value: Any) -> Optional[Decimal]:
    """검사 종류에 맞는 점수인지 검사하고 Decimal로 돌려준다. null은 None.

    assessment_type은 normalize_assessment_type()을 거친 표준 이름이어야 한다.
    수정할 때는 user_functions.update_assessment()가 "수정 후 검사명 + 수정 후 점수"로 다시 부른다.
    """
    score = parse_score(value)
    if score is None:
        return None

    spec = ASSESSMENT_TYPES.get(assessment_type)
    if spec is None:  # 목록이 바뀌기 전에 저장된 검사명 등 — KeyError 대신 고칠 방법을 알려준다
        raise ValueError(f"지원하지 않는 검사명입니다: {assessment_type!r} — 검사명도 함께 수정해 주세요.")
    shown = _format_score(score)

    if "allowed" in spec:
        allowed = [Decimal(str(v)) for v in spec["allowed"]]
        if score not in allowed:
            choices = ", ".join(_format_score(v) for v in allowed)
            raise ValueError(f"{assessment_type} 점수는 {choices} 중 하나여야 합니다: {shown}")
        return score

    low, high = Decimal(str(spec["min"])), Decimal(str(spec["max"]))
    if not low <= score <= high:
        raise ValueError(f"{assessment_type} 점수 범위는 {_format_score(low)}~{_format_score(high)}입니다: {shown}")
    if spec["step"] is not None:
        step = Decimal(str(spec["step"]))
        if (score - low) % step != 0:
            unit = "정수" if step == 1 else f"{_format_score(step)} 단위"
            raise ValueError(f"{assessment_type} 점수는 {unit}로만 입력할 수 있습니다: {shown}")
    return score


def _null_to_empty_list(value: Any) -> Any:
    """출력 모델용 — DB의 TEXT[]가 NULL이면 빈 목록으로, 배열 안의 NULL 원소는 빼고 맞춘다.

    컬럼이 nullable이고 원소 제약도 없어서, 이 모듈의 함수가 아닌 경로(psql 수정, 시드, 덤프 복원)로
    들어온 행에 생길 수 있다. 한 행 때문에 목록 조회 전체가 깨지지 않게 한다.
    """
    if value is None:
        return []
    return [item for item in value if item is not None]


# ============================================================
# users (간병인 계정)
# ============================================================


class UserIn(BaseModel):
    """create_user()가 받는 회원가입 정보. (users 테이블 입력)

    id / created_at / updated_at은 DB가 채우므로 여기에 없다.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="이름(표시용). 앞뒤 공백을 지운다")
    email: str = Field(description="로그인 이메일. 소문자로 바꿔 저장한다. 중복 불가")
    phone_number: Optional[str] = Field(
        default=None,
        description="휴대폰 번호. '-' 없이 숫자만 저장한다 (010-1234-5678도 받는다). 선택 입력, 중복 불가",
    )

    @field_validator("name", mode="before")
    @classmethod
    def _check_name(cls, value: Any) -> str:
        return _required_text(value, "name")

    @field_validator("email", mode="before")
    @classmethod
    def _check_email(cls, value: Any) -> str:
        return normalize_email(value)

    @field_validator("phone_number", mode="before")
    @classmethod
    def _check_phone(cls, value: Any) -> Optional[str]:
        return _normalize_phone(value)


class UserUpdate(BaseModel):
    """update_user()가 받는 수정 정보. 보낸 필드만 바뀐다.

    - phone_number를 null로 보내면 번호 삭제
    - name / email은 NOT NULL이라 null을 거부한다
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, description="이름. null 불가")
    email: Optional[str] = Field(default=None, description="로그인 이메일. null 불가, 중복 불가")
    phone_number: Optional[str] = Field(default=None, description="휴대폰 번호. null이면 삭제")

    @field_validator("name", mode="before")
    @classmethod
    def _check_name(cls, value: Any) -> str:
        return _required_text(value, "name")

    @field_validator("email", mode="before")
    @classmethod
    def _check_email(cls, value: Any) -> str:
        return normalize_email(value)

    @field_validator("phone_number", mode="before")
    @classmethod
    def _check_phone(cls, value: Any) -> Optional[str]:
        return _normalize_phone(value)


class UserOut(BaseModel):
    """users 테이블 한 행 + 담당 환자 id 목록. create_user / get_user / update_user가 돌려준다."""

    id: UUID = Field(description="사용자 고유 id (users.id)")
    name: str
    email: str = Field(description="로그인 이메일 (소문자로 저장됨)")
    phone_number: Optional[str] = Field(default=None, description="'-' 없는 숫자만 (예: 01012345678)")
    created_at: datetime = Field(description="가입 시각")
    updated_at: datetime = Field(description="마지막 정보 수정 시각")
    patient_ids: list[UUID] = Field(
        default_factory=list,
        description="담당 환자 id 목록 (patient_profiles.id, 등록 순). users 컬럼이 아니라 조회 시 붙인다",
    )


class UserSummary(BaseModel):
    """list_users()가 돌려주는 사용자 목록 한 줄. (개발·관리·배치용 — API로 공개하지 않는다)

    이름은 중복될 수 있으므로 사람을 구분할 때는 email(중복 불가)이나 id를 본다.
    """

    id: UUID = Field(description="사용자 고유 id (users.id)")
    name: str
    email: str = Field(description="로그인 이메일 (중복 불가)")
    patient_count: int = Field(description="담당 환자 수")
    created_at: datetime = Field(description="가입 시각")


# ============================================================
# patient_profiles (환자)
# ============================================================


class PatientIn(BaseModel):
    """create_patient()가 받는 환자 정보. (patient_profiles 테이블 입력)

    간병인 1명이 여러 환자를 등록할 수 있다 (users 1 : N patient_profiles).
    """

    model_config = ConfigDict(extra="forbid")

    caregiver_id: UUID = Field(description="담당 간병인 (users.id)")
    name: Optional[str] = Field(default=None, description="환자 이름. 별칭 가능 (예: 어머니)")
    dementia_stage: Optional[str] = Field(
        default=None, description=f"진단 단계: {' / '.join(DEMENTIA_STAGES)}. 모르면 비운다"
    )
    diagnosis_date: Optional[date] = Field(default=None, description="진단 받은 날짜. 미래 날짜 불가")
    symptoms: list[str] = Field(default_factory=list, description="증상 목록 (예: 수면장애, 배회). 공백·중복 제거")
    interests: list[str] = Field(default_factory=list, description="관심 치료·관리 분야. 공백·중복 제거")

    @field_validator("name", mode="before")
    @classmethod
    def _check_name(cls, value: Any) -> Optional[str]:
        return _optional_text(value, "name")

    @field_validator("dementia_stage", mode="before")
    @classmethod
    def _check_stage(cls, value: Any) -> Optional[str]:
        return _normalize_stage(value)

    @field_validator("diagnosis_date", mode="before")
    @classmethod
    def _check_diagnosis_date(cls, value: Any) -> Optional[date]:
        return parse_optional_date(value, "diagnosis_date")

    @field_validator("symptoms", "interests", mode="before")
    @classmethod
    def _check_lists(cls, value: Any, info: ValidationInfo) -> list[str]:
        return _text_list(value, info.field_name)


class PatientUpdate(BaseModel):
    """update_patient()가 받는 수정 정보. 보낸 필드만 바뀐다.

    - caregiver_id는 바꿀 수 없다 (담당 간병인 변경은 권한이 바뀌는 일이라 별도 기능으로 다룬다)
    - name / dementia_stage / diagnosis_date를 null로 보내면 값 삭제
    - symptoms / interests는 목록 전체를 새 목록으로 바꾼다 (null이면 빈 목록)
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    dementia_stage: Optional[str] = Field(default=None, description=f"{' / '.join(DEMENTIA_STAGES)}")
    diagnosis_date: Optional[date] = Field(default=None, description="미래 날짜 불가")
    symptoms: Optional[list[str]] = None
    interests: Optional[list[str]] = None

    @field_validator("name", mode="before")
    @classmethod
    def _check_name(cls, value: Any) -> Optional[str]:
        return _optional_text(value, "name")

    @field_validator("dementia_stage", mode="before")
    @classmethod
    def _check_stage(cls, value: Any) -> Optional[str]:
        return _normalize_stage(value)

    @field_validator("diagnosis_date", mode="before")
    @classmethod
    def _check_diagnosis_date(cls, value: Any) -> Optional[date]:
        return parse_optional_date(value, "diagnosis_date")

    @field_validator("symptoms", "interests", mode="before")
    @classmethod
    def _check_lists(cls, value: Any, info: ValidationInfo) -> list[str]:
        return _text_list(value, info.field_name)


class PatientOut(BaseModel):
    """patient_profiles 테이블 한 행. create_patient / get_patient / list_patients / update_patient가 돌려준다."""

    id: UUID = Field(description="환자 고유 id (patient_profiles.id)")
    caregiver_id: UUID = Field(description="담당 간병인 (users.id)")
    name: Optional[str] = None
    dementia_stage: Optional[str] = None
    diagnosis_date: Optional[date] = None
    symptoms: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    created_at: datetime = Field(description="등록 시각")
    updated_at: datetime = Field(description="마지막 수정 시각")

    @field_validator("symptoms", "interests", mode="before")
    @classmethod
    def _null_lists(cls, value: Any) -> Any:
        return _null_to_empty_list(value)


# ============================================================
# caregiver_profiles (간병인 자가점검)
# ============================================================


class CaregiverProfileIn(BaseModel):
    """upsert_caregiver_profile()이 받는 자가점검 정보. (caregiver_profiles 테이블 입력)

    사용자당 1행(user_id가 PK)이라, user_id는 함수 인자로 따로 받는다.
    모든 필드가 선택이고, 처음이면 새로 만들고 이미 있으면 **보낸 필드만** 바꾼다.
    """

    model_config = ConfigDict(extra="forbid")

    relationship: Optional[str] = Field(default=None, description="환자와의 관계 (예: 자녀, 배우자)")
    burden_score: Optional[int] = Field(
        default=None,
        description=f"Zarit 부담척도 점수. {BURDEN_SCORE_RANGE[0]}~{BURDEN_SCORE_RANGE[1]} 정수",
    )
    mood_score: Optional[int] = Field(
        default=None,
        description=f"PHQ-9 우울 셀프체크 점수. {MOOD_SCORE_RANGE[0]}~{MOOD_SCORE_RANGE[1]} 정수",
    )
    lifestyle_tags: Optional[list[str]] = Field(
        default=None, description="생활습관 태그 (예: 직장병행, 수면부족). 목록 전체를 새 목록으로 바꾼다"
    )

    @field_validator("relationship", mode="before")
    @classmethod
    def _check_relationship(cls, value: Any) -> Optional[str]:
        return _optional_text(value, "relationship")

    @field_validator("burden_score", mode="before")
    @classmethod
    def _check_burden(cls, value: Any) -> Optional[int]:
        return _int_in_range(value, "burden_score", BURDEN_SCORE_RANGE)

    @field_validator("mood_score", mode="before")
    @classmethod
    def _check_mood(cls, value: Any) -> Optional[int]:
        return _int_in_range(value, "mood_score", MOOD_SCORE_RANGE)

    @field_validator("lifestyle_tags", mode="before")
    @classmethod
    def _check_tags(cls, value: Any) -> list[str]:
        return _text_list(value, "lifestyle_tags")


class CaregiverProfileOut(BaseModel):
    """caregiver_profiles 테이블 한 행. upsert_caregiver_profile / get_caregiver_profile이 돌려준다."""

    user_id: UUID = Field(description="간병인 (users.id)")
    relationship: Optional[str] = None
    burden_score: Optional[int] = None
    mood_score: Optional[int] = None
    lifestyle_tags: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(description="마지막 자가점검 시각")

    @field_validator("lifestyle_tags", mode="before")
    @classmethod
    def _null_lists(cls, value: Any) -> Any:
        return _null_to_empty_list(value)


# ============================================================
# clinical_assessments (치매 평가 이력)
# ============================================================


class AssessmentIn(BaseModel):
    """create_assessment()가 받는 평가 결과. (clinical_assessments 테이블 입력)

    환자 1명이 여러 번 평가받는다 (평가 1회 = 1행, 같은 날 여러 척도면 척도마다 1행).
    검사명은 표준 이름으로 바꿔 저장하고, 점수는 ASSESSMENT_TYPES의 검사별 범위·단위로 검사한다.
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: UUID = Field(description="평가 대상 환자 (patient_profiles.id)")
    assessment_type: str = Field(description="검사명 (ASSESSMENT_TYPES의 표준 이름 또는 별칭, 예: k-mmse)")
    score: Optional[Decimal] = Field(
        default=None, description="점수. 검사별 범위·단위를 따른다. 판정만 기록할 때는 비운다"
    )
    result_detail: Optional[str] = Field(default=None, description="세부 결과·판정 (예: CDR 0.5, 기억력 영역 저하)")
    assessed_at: date = Field(description="평가 받은 날짜. 미래 날짜 불가")
    assessed_by: Optional[str] = Field(default=None, description="평가 주체 (예: 병원명, 치매안심센터, 보호자 자가평가)")

    @field_validator("assessment_type", mode="before")
    @classmethod
    def _check_type(cls, value: Any) -> str:
        return normalize_assessment_type(value)

    @field_validator("score", mode="before")
    @classmethod
    def _check_score_format(cls, value: Any) -> Optional[Decimal]:
        return parse_score(value)

    @field_validator("result_detail", "assessed_by", mode="before")
    @classmethod
    def _check_texts(cls, value: Any, info: ValidationInfo) -> Optional[str]:
        return _optional_text(value, info.field_name)

    @field_validator("assessed_at", mode="before")
    @classmethod
    def _check_assessed_at(cls, value: Any) -> date:
        return parse_date(value, "assessed_at")

    @model_validator(mode="after")
    def _check_score_range(self) -> "AssessmentIn":
        """검사명과 점수를 함께 봐야 하는 범위·단위 검사."""
        self.score = check_assessment_score(self.assessment_type, self.score)
        return self


class AssessmentUpdate(BaseModel):
    """update_assessment()가 받는 수정 정보 (입력 실수 정정용). 보낸 필드만 바뀐다.

    - patient_id는 바꿀 수 없다 (다른 환자로 옮기는 수정은 거부)
    - assessment_type / assessed_at은 null 불가, score는 null이면 점수 삭제
    - 점수 범위는 "수정 후 검사명 + 수정 후 점수"로 검사해야 해서
      (점수만 고치면 기존 검사명이 필요하다) user_functions.update_assessment()가 DB 값과 합쳐 검사한다.
    """

    model_config = ConfigDict(extra="forbid")

    assessment_type: Optional[str] = None
    score: Optional[Decimal] = None
    result_detail: Optional[str] = None
    assessed_at: Optional[date] = None
    assessed_by: Optional[str] = None

    @field_validator("assessment_type", mode="before")
    @classmethod
    def _check_type(cls, value: Any) -> str:
        return normalize_assessment_type(value)

    @field_validator("score", mode="before")
    @classmethod
    def _check_score_format(cls, value: Any) -> Optional[Decimal]:
        return parse_score(value)

    @field_validator("result_detail", "assessed_by", mode="before")
    @classmethod
    def _check_texts(cls, value: Any, info: ValidationInfo) -> Optional[str]:
        return _optional_text(value, info.field_name)

    @field_validator("assessed_at", mode="before")
    @classmethod
    def _check_assessed_at(cls, value: Any) -> date:
        return parse_date(value, "assessed_at")


class AssessmentOut(BaseModel):
    """clinical_assessments 테이블 한 행. 평가 함수들이 돌려준다."""

    id: UUID = Field(description="평가 기록 고유 id (clinical_assessments.id)")
    patient_id: UUID = Field(description="평가 대상 환자 (patient_profiles.id)")
    assessment_type: str = Field(description="검사 표준 이름")
    score: Optional[float] = Field(default=None, description="점수 (DB NUMERIC → float)")
    result_detail: Optional[str] = None
    assessed_at: date
    assessed_by: Optional[str] = None
    created_at: datetime = Field(description="기록 입력 시각")


class AssessmentTypeInfo(BaseModel):
    """list_assessment_types()가 돌려주는 검사 한 종류의 점수 규칙. (DB 테이블 아님, ASSESSMENT_TYPES)

    앱의 검사 선택 목록·점수 입력 제한, AI의 점수 해석(높을수록 나쁜지)에 쓴다.
    """

    assessment_type: str = Field(description="검사 표준 이름 (예: K-MMSE)")
    name: str = Field(description="검사 이름(한글 설명)")
    min: Optional[float] = Field(default=None, description="최저 점수")
    max: Optional[float] = Field(default=None, description="최고 점수")
    step: Optional[float] = Field(
        default=None,
        description="점수 단위 (1 = 정수, 0.5 = 0.5 단위). allowed가 없고 null이면 소수 둘째 자리까지 자유, "
        "allowed가 있으면 null",
    )
    allowed: Optional[list[float]] = Field(
        default=None,
        description="정해진 값만 허용하는 검사(CDR)의 허용 값. 있으면 이 값들만 허용 (min/max는 그 최솟값·최댓값)",
    )
    higher_is_worse: bool = Field(description="점수가 높을수록 나쁜 상태인지")


# ============================================================
# safety_events (안전·행동 이벤트)
# ============================================================


class SafetyEventIn(BaseModel):
    """upsert_safety_event()가 받는 하루치 안전·행동 기록. (safety_events 테이블 입력)

    환자당 하루 1행 (UNIQUE (patient_id, event_date)). 그날 기록이 없으면 만들고,
    있으면 **보낸 필드만** 바꾼다 (오전에 낙상, 저녁에 배회를 체크하면 둘 다 남는다).
    patient_id + event_date만 보내면 "기록했고 이상 없음"이 된다.
      - 행이 없는 날 = 기록하지 않은 날 / 행이 있고 전부 false = 이상 없는 날
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: UUID = Field(description="대상 환자 (patient_profiles.id)")
    event_date: date = Field(description="기록 대상 날짜. 미래 날짜 불가")
    has_fall: bool = Field(default=False, description="낙상 여부 (true/false만)")
    has_wandering: bool = Field(default=False, description="배회 여부 (true/false만)")
    has_missing: bool = Field(default=False, description="실종 여부 (true/false만)")
    note: Optional[str] = Field(default=None, description="상황 메모. null이면 메모 삭제")

    @field_validator("event_date", mode="before")
    @classmethod
    def _check_event_date(cls, value: Any) -> date:
        return parse_date(value, "event_date")

    @field_validator("has_fall", "has_wandering", "has_missing", mode="before")
    @classmethod
    def _check_flags(cls, value: Any, info: ValidationInfo) -> bool:
        return _strict_bool(value, info.field_name)

    @field_validator("note", mode="before")
    @classmethod
    def _check_note(cls, value: Any) -> Optional[str]:
        return _optional_text(value, "note")


class SafetyEventOut(BaseModel):
    """safety_events 테이블 한 행. 안전·행동 이벤트 함수들이 돌려준다."""

    id: UUID = Field(description="이벤트 기록 고유 id (safety_events.id)")
    patient_id: UUID = Field(description="대상 환자 (patient_profiles.id)")
    event_date: date
    has_fall: bool
    has_wandering: bool
    has_missing: bool
    note: Optional[str] = None
    created_at: datetime = Field(description="처음 기록한 시각 (upsert로 갱신해도 바뀌지 않는다)")


# ============================================================
# medications (복약 정보)
# ============================================================


class MedicationIn(BaseModel):
    """create_medication()이 받는 복약 정보. (medications 테이블 입력)

    약 1종 = 1행. 복용을 멈추면 지우지 않고 is_taking=false + end_date로 남긴다 (복약 이력).
    복용 시작·종료일은 미래도 받는다 (내일부터 먹을 약, 2주 뒤 끝나는 처방).
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: UUID = Field(description="복용 환자 (patient_profiles.id)")
    drug_name: str = Field(description="약품명 (예: 아리셉트정 5mg, 도네페질)")
    dosage: Optional[str] = Field(default=None, description="1회 복용량 (예: 1정, 5mg)")
    frequency: Optional[str] = Field(default=None, description="복약 주기 (예: 1일 1회 취침 전)")
    is_taking: bool = Field(default=True, description="현재 복용 여부 (true/false만, 기본 true)")
    start_date: Optional[date] = Field(default=None, description="복용 시작일 (미래 가능)")
    end_date: Optional[date] = Field(default=None, description="복용 종료일 (미래 가능, start_date 이후)")
    note: Optional[str] = Field(default=None, description="메모 (부작용, 처방 병원 등)")

    @field_validator("drug_name", mode="before")
    @classmethod
    def _check_drug_name(cls, value: Any) -> str:
        return _required_text(value, "drug_name")

    @field_validator("dosage", "frequency", "note", mode="before")
    @classmethod
    def _check_texts(cls, value: Any, info: ValidationInfo) -> Optional[str]:
        return _optional_text(value, info.field_name)

    @field_validator("is_taking", mode="before")
    @classmethod
    def _check_is_taking(cls, value: Any) -> bool:
        return _strict_bool(value, "is_taking")

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _check_dates(cls, value: Any, info: ValidationInfo) -> Optional[date]:
        return parse_optional_date(value, info.field_name, allow_future=True)

    @model_validator(mode="after")
    def _check_date_order(self) -> "MedicationIn":
        check_date_order(self.start_date, self.end_date, "start_date", "end_date")
        return self


class MedicationUpdate(BaseModel):
    """update_medication()이 받는 수정 정보. 보낸 필드만 바뀐다.

    - patient_id는 바꿀 수 없다
    - drug_name / is_taking은 null 불가, 다른 텍스트·날짜는 null이면 값 삭제
    - 복용 중단: {"is_taking": false, "end_date": "YYYY-MM-DD"}
    - 날짜 순서는 "수정 후 시작일 + 수정 후 종료일"로 검사해야 해서
      user_functions.update_medication()이 DB 값과 합쳐 검사한다.
    """

    model_config = ConfigDict(extra="forbid")

    drug_name: Optional[str] = None
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    is_taking: Optional[bool] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    note: Optional[str] = None

    @field_validator("drug_name", mode="before")
    @classmethod
    def _check_drug_name(cls, value: Any) -> str:
        return _required_text(value, "drug_name")

    @field_validator("dosage", "frequency", "note", mode="before")
    @classmethod
    def _check_texts(cls, value: Any, info: ValidationInfo) -> Optional[str]:
        return _optional_text(value, info.field_name)

    @field_validator("is_taking", mode="before")
    @classmethod
    def _check_is_taking(cls, value: Any) -> bool:
        return _strict_bool(value, "is_taking")

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _check_dates(cls, value: Any, info: ValidationInfo) -> Optional[date]:
        return parse_optional_date(value, info.field_name, allow_future=True)


class MedicationOut(BaseModel):
    """medications 테이블 한 행. 복약 함수들이 돌려준다."""

    id: UUID = Field(description="복약 정보 고유 id (medications.id)")
    patient_id: UUID = Field(description="복용 환자 (patient_profiles.id)")
    drug_name: str
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    is_taking: bool = Field(description="현재 복용 여부")
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    note: Optional[str] = None
    created_at: datetime = Field(description="등록 시각")
    updated_at: datetime = Field(description="마지막 수정 시각")


# ============================================================
# medical_visits (병원 방문 기록)
# ============================================================

# 진료 내용 텍스트 컬럼. visit_reason ~ follow_up_plan은 005에서 추가된 진료 항목,
# visit_content는 기타 자유 메모 (005에서 의미 변경).
VISIT_TEXT_FIELDS = (
    "hospital_name", "department", "visit_reason", "diagnosis", "treatment_content",
    "test_summary", "medication_change", "doctor_note", "follow_up_plan", "visit_content",
)


class MedicalVisitIn(BaseModel):
    """create_medical_visit()이 받는 병원 방문(또는 예약) 정보. (medical_visits 테이블 입력)

    진료 1회 = 1행. 예약만 한 방문은 is_visited=false로 넣고, 다녀오면 true + 진료 내용으로 바꾼다.
    visit_date는 예약이라 미래도 받지만, 미래 날짜를 방문 완료(is_visited=true)로 넣을 수는 없다.
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: UUID = Field(description="진료 받은 환자 (patient_profiles.id)")
    visit_date: date = Field(description="방문(예정)일. 예약이면 미래 가능")
    is_visited: bool = Field(default=False, description="실제 방문 여부 (true/false만, 기본 false = 예약)")
    hospital_name: Optional[str] = Field(default=None, description="병원명")
    department: Optional[str] = Field(default=None, description="진료과 (예: 신경과, 정신건강의학과)")
    visit_reason: Optional[str] = Field(default=None, description="방문 목적 (예: 정기 진료, 약 조절)")
    diagnosis: Optional[str] = Field(default=None, description="진단 / 의사 소견")
    treatment_content: Optional[str] = Field(default=None, description="진료 / 처치 내용")
    test_summary: Optional[str] = Field(default=None, description="검사 내용 요약 (예: MRI 해마 위축 소견)")
    medication_change: Optional[str] = Field(default=None, description="약 처방 / 변경 사항")
    doctor_note: Optional[str] = Field(default=None, description="의사 전달사항 (보호자에게 당부한 내용)")
    follow_up_plan: Optional[str] = Field(default=None, description="다음 진료 계획")
    visit_content: Optional[str] = Field(default=None, description="기타 자유 메모")

    @field_validator("visit_date", mode="before")
    @classmethod
    def _check_visit_date(cls, value: Any) -> date:
        return parse_date(value, "visit_date", allow_future=True)

    @field_validator("is_visited", mode="before")
    @classmethod
    def _check_is_visited(cls, value: Any) -> bool:
        return _strict_bool(value, "is_visited")

    @field_validator(*VISIT_TEXT_FIELDS, mode="before")
    @classmethod
    def _check_texts(cls, value: Any, info: ValidationInfo) -> Optional[str]:
        return _optional_text(value, info.field_name)

    @model_validator(mode="after")
    def _check_visit_state(self) -> "MedicalVisitIn":
        check_visit_state(self.visit_date, self.is_visited)
        return self


class MedicalVisitUpdate(BaseModel):
    """update_medical_visit()이 받는 수정 정보. 보낸 필드만 바뀐다.

    다녀온 뒤에는 {"is_visited": true, "diagnosis": "...", "medication_change": "...", ...}로 채운다.
    - patient_id는 바꿀 수 없다, visit_date / is_visited는 null 불가
    - 진료 항목 텍스트는 null이면 값 삭제
    - "미래 날짜인데 방문 완료"는 수정 후 visit_date + is_visited로 검사해야 해서
      user_functions.update_medical_visit()이 DB 값과 합쳐 검사한다.
    """

    model_config = ConfigDict(extra="forbid")

    visit_date: Optional[date] = None
    is_visited: Optional[bool] = None
    hospital_name: Optional[str] = None
    department: Optional[str] = None
    visit_reason: Optional[str] = None
    diagnosis: Optional[str] = None
    treatment_content: Optional[str] = None
    test_summary: Optional[str] = None
    medication_change: Optional[str] = None
    doctor_note: Optional[str] = None
    follow_up_plan: Optional[str] = None
    visit_content: Optional[str] = None

    @field_validator("visit_date", mode="before")
    @classmethod
    def _check_visit_date(cls, value: Any) -> date:
        return parse_date(value, "visit_date", allow_future=True)

    @field_validator("is_visited", mode="before")
    @classmethod
    def _check_is_visited(cls, value: Any) -> bool:
        return _strict_bool(value, "is_visited")

    @field_validator(*VISIT_TEXT_FIELDS, mode="before")
    @classmethod
    def _check_texts(cls, value: Any, info: ValidationInfo) -> Optional[str]:
        return _optional_text(value, info.field_name)


class MedicalVisitOut(BaseModel):
    """medical_visits 테이블 한 행. 병원 방문 함수들이 돌려준다."""

    id: UUID = Field(description="방문 기록 고유 id (medical_visits.id)")
    patient_id: UUID = Field(description="진료 받은 환자 (patient_profiles.id)")
    visit_date: date
    is_visited: bool
    hospital_name: Optional[str] = None
    department: Optional[str] = None
    visit_reason: Optional[str] = None
    diagnosis: Optional[str] = None
    treatment_content: Optional[str] = None
    test_summary: Optional[str] = None
    medication_change: Optional[str] = None
    doctor_note: Optional[str] = None
    follow_up_plan: Optional[str] = None
    visit_content: Optional[str] = None
    created_at: datetime = Field(description="기록 입력 시각")
    updated_at: datetime = Field(description="마지막 수정 시각")


# ============================================================
# 프로필 묶음 (get_user_profile_context) — AI 피드·챗봇 입력용
# ============================================================
# 테이블이 아니라 위의 XxxOut을 한 사용자 기준으로 묶은 것이다.
# AI 팀이 이 형식을 그대로 받아 쓰므로, 필드를 바꾸면 팀에 공지하고 AGENTS.md도 같이 고친다.
# JSON 텍스트가 필요하면 `.model_dump_json()`, dict가 필요하면 `.model_dump(mode="json")`.


class PatientContext(BaseModel):
    """환자 한 명의 현재 상태 묶음. UserProfileContext.patients의 한 항목."""

    patient: PatientOut = Field(description="환자 기본 정보 (단계·진단일·증상·관심 분야)")
    latest_assessments: list[AssessmentOut] = Field(
        default_factory=list, description="검사별 가장 최근 평가 1건씩 (assessment_type 이름순)",
    )
    current_medications: list[MedicationOut] = Field(
        default_factory=list, description="현재 복용 중인 약만 (is_taking = true)",
    )
    recent_safety_events: list[SafetyEventOut] = Field(
        default_factory=list,
        description=f"최근 {PROFILE_SAFETY_DAYS}일 안전·행동 기록 중 낙상·배회·실종이 하나라도 있는 날만 (최근 날짜부터)",
    )
    recent_visits: list[MedicalVisitOut] = Field(
        default_factory=list, description=f"다녀온 진료 최근 {PROFILE_RECENT_VISITS}건 (최근 날짜부터)",
    )
    upcoming_visits: list[MedicalVisitOut] = Field(
        default_factory=list, description="오늘 이후 진료 예약 (가까운 날짜부터)",
    )


class CaregiverContext(BaseModel):
    """get_caregiver_context()가 돌려주는 간병인 정보 (환자 상세 없이).

    환자 상세는 patient_ids의 id로 get_patient_context()를 부른다.
    email / phone_number는 담지 않는다 (UserProfileContext와 같은 기준).
    """

    user_id: UUID = Field(description="사용자 고유 id (users.id)")
    name: str = Field(description="사용자(간병인) 이름")
    caregiver_profile: Optional[CaregiverProfileOut] = Field(
        default=None, description="간병인 자가점검. 아직 저장한 적이 없으면 null",
    )
    patient_ids: list[UUID] = Field(default_factory=list, description="담당 환자 id 목록 (등록 순)")
    generated_at: datetime = Field(description="조회 시각 (한국 시간)")


class UserProfileContext(BaseModel):
    """get_user_profile_context()가 돌려주는 사용자 한 명의 프로필 묶음.

    개인정보 최소화를 위해 email / phone_number는 담지 않는다 (AI·LLM에 넘길 필요가 없다).
    """

    user_id: UUID = Field(description="사용자 고유 id (users.id)")
    name: str = Field(description="사용자(간병인) 이름")
    caregiver_profile: Optional[CaregiverProfileOut] = Field(
        default=None, description="간병인 자가점검. 아직 저장한 적이 없으면 null",
    )
    patients: list[PatientContext] = Field(default_factory=list, description="담당 환자들 (등록 순)")
    generated_at: datetime = Field(description="이 묶음을 조회한 시각 (한국 시간)")
