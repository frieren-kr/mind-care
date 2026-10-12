"""DB의 근거 등급(문자 "1"~"6")을 화면용 A~D로 바꿔 응답에 채운다. (박주현 담당)

★ 이 변환이 API 계층에 있는 이유 ★
A~D 규칙(`GRADE`)의 정본은 AI 담당(김현서)의 `app/ai/retrieval/evidence.py`다.
그런데 `app/db/` 계층은 `app/ai/`를 import 하지 않는다 (db → ai 방향 의존을 만들지 않는다).
그래서 조회 함수는 DB 값(`evidence_level`)만 그대로 돌려주고, 등급 변환은
응답을 만드는 이 계층에서 한다.

★ 왜 그냥 넘기면 안 되는가 ★
`to_grade()`는 **정수** 1~6을 받는데(`GRADE = {1: "A", ...}`), DB의
`paper_analysis.evidence_level`은 **문자열** ("1"~"6")이다. 문자열을 그대로 넣으면
`dict.get()`이 못 찾아서 조용히 None이 나온다. 그래서 여기서 정수로 바꿔 넘긴다.

`evidence_level` 자체는 응답에서 그대로 유지한다 (등급을 덧붙이기만 한다).
"""
import logging
from typing import Optional, TypeVar

from pydantic import BaseModel

from app.ai.retrieval.evidence import to_grade

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


def grade_of(evidence_level: Optional[str]) -> Optional[str]:
    """DB의 문자 evidence_level("1"~"6") → 화면용 등급 "A"~"D".

    비어 있거나 숫자가 아니거나 1~6 밖이면 None(등급 미확인)을 돌려준다.
    요약이 아직 없는 논문, 등급을 매기지 않는 연구(동물 연구 등)가 여기 해당한다.

    예) grade_of("2") → "B"   grade_of(None) → None   grade_of("") → None
    """
    if evidence_level is None:
        return None
    text = evidence_level.strip()
    if not text:
        return None
    try:
        level = int(text)
    except ValueError:
        # 등급 체계가 바뀌어 숫자가 아닌 값이 들어온 경우. 응답을 깨지 않고 미확인으로 둔다.
        logger.warning("evidence_level이 숫자가 아닙니다: %r (등급 없음으로 처리)", evidence_level)
        return None
    return to_grade(level)


def with_grade(item: ModelT) -> ModelT:
    """evidence_level을 보고 evidence_grade를 채운 사본을 돌려준다.

    `item`은 `evidence_level`과 `evidence_grade` 칸을 가진 응답 모델이어야 한다
    (PaperListItem / PaperDetailResponse / RecommendationItem).
    """
    return item.model_copy(update={"evidence_grade": grade_of(item.evidence_level)})


def with_grades(items: list[ModelT]) -> list[ModelT]:
    """목록의 모든 항목에 evidence_grade를 채운다."""
    return [with_grade(item) for item in items]
