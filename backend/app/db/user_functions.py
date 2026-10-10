"""사용자·환자 도메인 데이터 접근 함수 (김한슬 담당).

★ 팀 규칙 ★
AI/백엔드 팀원은 DB에 직접 SQL을 쓰지 않는다.
사용자·환자 테이블은 반드시 이 모듈의 함수를 통해서만 접근한다. (논문 테이블은 functions.py)
함수 이름 / 입출력 형식이 바뀌면 반드시 AGENTS.md의 "데이터 접근 계약"에 기록한다.

입출력 형식은 `app/user_schemas.py`가 정본이다 (논문 쪽 functions.py ↔ schemas.py와 같은 관계).
- 입력: JSON을 파싱한 dict 그대로 넣어도 되고, user_schemas의 모델(XxxIn / XxxUpdate)을 넣어도 된다.
  dict는 그 모델로 검증한다. id 인자는 UUID 객체나 UUID 문자열 모두 받는다.
- 출력: user_schemas의 XxxOut 모델. API에서 JSON으로 보낼 때는 FastAPI가 자동으로 바꾸고,
  직접 바꿀 때는 `.model_dump(mode="json")` (UUID·날짜가 문자열이 된다).

설계 원칙:
- [functions.py와 같음] DB 접근은 UUID 기준이다 (사용자·환자·기록 모두 id로 조회·수정한다).
- [functions.py와 같음] id / created_at / updated_at은 DB가 채운다. 저장 후 RETURNING으로 받아 돌려준다.
- [functions.py와 같음] 입력이 잘못되면 DB에 가기 전에 ValueError(한글 메시지)를 던진다.
  형식 검사는 user_schemas의 모델이, DB의 현재 값이 필요한 검사(수정 후 값 조합)는 이 모듈이 맡는다.
- [이 모듈 규칙] 조회·수정 대상이 없거나 id 형식이 틀리면 예외 대신 None(목록은 빈 목록)을 돌려준다.
- [이 모듈 규칙] DB 제약 위반(없는 환자·사용자 = 외래키, 이메일·번호 중복 = UNIQUE)도
  의미 있는 ValueError로 바꿔 던진다 (사용자 입력이라 API가 메시지를 그대로 보여줄 수 있게).
- 날짜의 "미래" 판정은 한국 시간 기준이다 (user_schemas.today()).
- 권한(이 사용자가 이 환자의 간병인인가)은 보지 않는다. API가 is_caregiver_of()로 먼저 확인한다.

테이블 정의는 migrations/001_init.sql(users, patient_profiles, caregiver_profiles)
+ 004_patient_management.sql(phone_number, clinical_assessments, safety_events, medications, medical_visits)
+ 005_medical_visits_detail.sql(medical_visits 진료 항목 컬럼, 인덱스)이 정본이다.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional, TypeVar
from uuid import UUID

from psycopg import sql
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg.rows import dict_row
from pydantic import BaseModel, ValidationError

from app.db.connection import get_connection
from app.user_schemas import (
    APP_TIMEZONE,
    ASSESSMENT_TYPES,
    PROFILE_RECENT_VISITS,
    PROFILE_SAFETY_DAYS,
    AssessmentIn,
    AssessmentOut,
    AssessmentTypeInfo,
    AssessmentUpdate,
    CaregiverContext,
    CaregiverProfileIn,
    CaregiverProfileOut,
    MedicalVisitIn,
    MedicalVisitOut,
    MedicalVisitUpdate,
    MedicationIn,
    MedicationOut,
    MedicationUpdate,
    PatientContext,
    PatientIn,
    PatientOut,
    PatientUpdate,
    SafetyEventIn,
    SafetyEventOut,
    UserIn,
    UserOut,
    UserProfileContext,
    UserSummary,
    UserUpdate,
    check_assessment_score,
    check_date_order,
    check_visit_state,
    normalize_assessment_type,
    parse_optional_date,
    today,
)

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


# ============================================================
# 공통 헬퍼 — 입력 검증
# ============================================================

# pydantic이 기본으로 내는 영문 에러를 한글 메시지로 바꾼다 (에러 종류 → 문구).
# 우리 validator가 던진 ValueError(type="value_error")는 이미 한글이라 그대로 쓴다.
_ERROR_MESSAGES = {
    "uuid_parsing": "UUID 형식이 아닙니다",
    "uuid_type": "UUID 형식이 아닙니다",
    "string_type": "문자열이어야 합니다",
    "bool_type": "true/false여야 합니다",
    "int_type": "정수여야 합니다",
    "list_type": "목록이어야 합니다",
    "date_type": "'YYYY-MM-DD' 형식이어야 합니다",
    "date_parsing": "'YYYY-MM-DD' 형식이어야 합니다",
}


def _validation_message(error: ValidationError, model: type[BaseModel]) -> str:
    """pydantic ValidationError를 한글 한 줄 메시지로 바꾼다 (여러 건이면 '; '로 잇는다)."""
    allowed = ", ".join(sorted(model.model_fields))
    unknown: list[str] = []
    messages: list[str] = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "입력"
        kind = item["type"]
        if kind == "extra_forbidden":
            unknown.append(field)
        elif kind == "missing":
            messages.append(f"필수 필드가 없습니다: {field}")
        elif kind == "value_error":
            messages.append(str(item["ctx"]["error"]))
        elif kind in _ERROR_MESSAGES:
            messages.append(f"{field}: {_ERROR_MESSAGES[kind]} ({item.get('input')!r})")
        else:
            messages.append(f"{field}: {item['msg']}")
    if unknown:
        messages.insert(0, f"알 수 없는 필드입니다: {', '.join(sorted(unknown))} (허용: {allowed})")
    return "; ".join(messages)


def _to_model(model: type[ModelT], data: Any) -> ModelT:
    """입력을 user_schemas 모델로 바꾼다. 이미 그 모델이면 그대로, dict면 검증한다.

    JSON을 파싱한 dict(앱 요청 본문)와 모델 객체(AI·배치 코드)를 모두 받기 위한 관문이다.
    검증에 실패하면 pydantic의 영문 에러 대신 한글 ValueError를 던진다
    (ValidationError도 ValueError의 하위 클래스지만 메시지를 팀 규칙에 맞춘다).
    """
    if isinstance(data, model):
        return data
    if not isinstance(data, dict):
        raise ValueError(f"입력은 dict(JSON 객체) 또는 {model.__name__}여야 합니다: {type(data).__name__}")
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise ValueError(_validation_message(error, model)) from error


def _parse_uuid(value: Any) -> Optional[UUID]:
    """조회·수정 대상 id — UUID 객체나 UUID 문자열을 UUID로 바꾼다. 형식이 아니면 None.

    형식이 틀린 id로는 어떤 행도 찾을 수 없으므로, 조회·수정 함수는 None이면 DB에 가지 않고
    "대상 없음"(None / 빈 목록)을 돌려준다. (저장할 값으로 들어오는 id는 모델이 검사한다.)
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _check_flag(value: Any, field: str) -> Optional[bool]:
    """조회 필터용 true/false — None은 필터 없음. "true" 문자열·1/0은 거부한다."""
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{field}는 true/false여야 합니다: {value!r}")
    return value


def _columns(model: type[BaseModel], *, exclude: frozenset[str] = frozenset()) -> str:
    """출력 모델의 필드 이름으로 SELECT 컬럼 목록을 만든다.

    XxxOut 모델의 필드 이름은 DB 컬럼 이름과 같다 — 계약(user_schemas)에 필드를 추가하면
    조회 컬럼도 자동으로 따라온다. 컬럼이 아닌 필드(UserOut.patient_ids)는 exclude로 뺀다.
    """
    return ", ".join(name for name in model.model_fields if name not in exclude)


# ============================================================
# 공통 헬퍼 — DB 접근
# ============================================================

# UNIQUE 제약(인덱스) 이름 → 사용자에게 보여줄 메시지.
# users_email_key는 001의 `email TEXT UNIQUE`가 자동으로 만든 이름,
# users_phone_number_key는 004에서 만든 UNIQUE INDEX 이름이다.
_UNIQUE_MESSAGES = {
    "users_email_key": "이미 사용 중인 이메일입니다.",
    "users_phone_number_key": "이미 사용 중인 휴대폰 번호입니다.",
}


def _unique_violation_message(error: UniqueViolation) -> str:
    """UNIQUE 위반 예외를 어떤 값이 겹쳤는지 알려주는 메시지로 바꾼다."""
    constraint = error.diag.constraint_name or ""
    return _UNIQUE_MESSAGES.get(constraint, f"이미 존재하는 값입니다 ({constraint}).")


def _insert_row(table: str, values: dict[str, Any]) -> dict[str, Any]:
    """한 행을 INSERT 하고 저장된 행(RETURNING *)을 dict로 돌려준다.

    values에 없는 컬럼은 DB 기본값(id, created_at, DEFAULT false 등)이 채운다.
    외래키 위반(없는 환자·사용자)은 호출하는 쪽에서 ValueError로 바꾼다.
    """
    query = sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING *").format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in values),
        values=sql.SQL(", ").join(sql.Placeholder(column) for column in values),
    )
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, values)
            return cur.fetchone()


def _update_row(
    table: str,
    key_column: str,
    key: UUID,
    fields: dict[str, Any],
    *,
    touch_updated_at: bool = True,
) -> Optional[dict[str, Any]]:
    """한 행에서 `fields`에 있는 컬럼만 UPDATE 하고 갱신된 행을 dict로 돌려준다.

    - fields: 바꿀 컬럼과 값. 수정 모델에서 "실제로 보낸 필드만"(exclude_unset) 넘긴다
      → 보내지 않은 컬럼은 그대로 남는다.
    - touch_updated_at: updated_at 컬럼이 있는 테이블이면 now()로 다시 찍는다
      (스키마에 트리거가 없어서 함수가 직접 갱신한다).

    대상 행이 없으면 None. fields가 비어 있으면 호출하는 쪽이 DB에 가지 않도록 처리한다.
    table / key_column / fields의 키는 모델 필드 이름에서만 오지만,
    sql.Identifier로 감싸 컬럼·테이블 이름도 안전하게 이스케이프한다.
    """
    assignments = [
        sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder(column))
        for column in fields
    ]
    if touch_updated_at:
        assignments.append(sql.SQL("updated_at = now()"))

    query = sql.SQL("UPDATE {table} SET {assignments} WHERE {key_column} = %(_key)s RETURNING *").format(
        table=sql.Identifier(table),
        assignments=sql.SQL(", ").join(assignments),
        key_column=sql.Identifier(key_column),
    )
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {**fields, "_key": key})
            return cur.fetchone()


def _upsert_row(
    table: str,
    conflict_columns: list[str],
    values: dict[str, Any],
    update_columns: list[str],
    *,
    touch_updated_at: bool = True,
) -> dict[str, Any]:
    """한 행을 upsert 하고 저장된 행을 돌려준다. (환자당 하루 1건·사용자당 1건인 테이블용)

    - 행이 없으면: values로 INSERT
    - 행이 있으면(conflict_columns 충돌): update_columns에 든 컬럼만 새 값으로 바꾼다
      → 입력에 없던 컬럼은 기존 값을 유지한다 (예: 낙상만 체크해도 배회 기록은 그대로).
    - update_columns가 비어 있으면 기존 행을 그대로 돌려준다
      (ON CONFLICT DO NOTHING은 RETURNING에 행을 주지 않아서, 값이 같은 no-op UPDATE를 쓴다).
    """
    assignments = [
        sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(column)) for column in update_columns
    ]
    if touch_updated_at:
        assignments.append(sql.SQL("updated_at = now()"))
    if not assignments:
        first = sql.Identifier(conflict_columns[0])
        assignments.append(sql.SQL("{col} = EXCLUDED.{col}").format(col=first))

    query = sql.SQL(
        "INSERT INTO {table} ({columns}) VALUES ({values}) "
        "ON CONFLICT ({conflict}) DO UPDATE SET {assignments} RETURNING *"
    ).format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in values),
        values=sql.SQL(", ").join(sql.Placeholder(column) for column in values),
        conflict=sql.SQL(", ").join(sql.Identifier(column) for column in conflict_columns),
        assignments=sql.SQL(", ").join(assignments),
    )
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, values)
            return cur.fetchone()


def _fetch_one(query: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
    """SELECT 결과 한 행을 dict로. 없으면 None."""
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchone()


def _fetch_all(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """SELECT 결과 전체를 dict 목록으로."""
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall()


# ============================================================
# users (간병인 계정)
# ============================================================

_USER_COLUMNS = _columns(UserOut, exclude=frozenset({"patient_ids"}))

# 담당 환자 id 목록. ARRAY(서브쿼리)는 환자가 없으면 빈 배열을 돌려준다 (NULL 아님).
# 정렬은 등록 순서(created_at) → id로 고정한다 — 같은 사용자를 다시 조회해도 순서가 같다.
# 탐색은 005의 idx_patient_profiles_caregiver (caregiver_id, created_at) 인덱스를 탄다
# (인덱스가 없으면 조회마다 patient_profiles 전체를 읽는다).
_USER_PATIENT_IDS = """
    ARRAY(
        SELECT p.id FROM patient_profiles AS p
         WHERE p.caregiver_id = u.id
         ORDER BY p.created_at, p.id
    ) AS patient_ids
"""


def create_user(user: dict[str, Any] | UserIn) -> UserOut:
    """회원가입 — users에 새 사용자를 저장하고 저장된 행을 돌려준다.

    id는 DB가 만든다 (DEFAULT uuid_generate_v4()). 저장 직후 화면에 바로 쓸 수 있게
    id만이 아니라 행 전체를 돌려준다.

    Args:
        user: 회원가입 정보 (dict 또는 user_schemas.UserIn).
            {"name": "김한슬", "email": "a@b.com", "phone_number": "010-1234-5678"}
            - name, email: 필수. 이메일은 소문자로 바꿔 저장한다.
            - phone_number: 선택. '-' 없이 숫자만 저장한다.

    Returns:
        저장된 UserOut (DB가 만든 id, created_at, updated_at 포함).
        막 가입했으므로 patient_ids는 빈 목록이다.

    Raises:
        ValueError: 필드가 빠졌거나 형식이 틀린 경우, 모르는 필드가 있는 경우,
            이메일·휴대폰 번호가 이미 다른 사용자에게 등록된 경우.
    """
    data = _to_model(UserIn, user)

    try:
        row = _insert_row("users", data.model_dump())
    except UniqueViolation as error:
        raise ValueError(_unique_violation_message(error)) from error

    logger.info("create_user: user_id=%s 저장", row["id"])
    return UserOut.model_validate(row)  # 막 가입한 사용자라 patient_ids는 기본값 []


def get_user(user_id: UUID | str) -> Optional[UserOut]:
    """user_id로 사용자(간병인) 한 명의 계정 정보를 조회한다.

    users 한 행 + 담당 환자의 id 목록(patient_ids)을 돌려준다.
    환자는 id만 준다 — 이름·단계 등 상세는 그 id로 get_patient()를 부른다.
    간병인 자가점검은 get_caregiver_profile()로 따로 조회한다.

    Args:
        user_id: 조회할 사용자 (users.id). UUID 또는 UUID 문자열.

    Returns:
        UserOut. 해당 user_id가 없거나 id 형식이 틀리면 None (예외를 던지지 않는다).
    """
    key = _parse_uuid(user_id)
    if key is None:
        return None

    row = _fetch_one(
        f"SELECT {_USER_COLUMNS}, {_USER_PATIENT_IDS} FROM users AS u WHERE u.id = %(user_id)s",
        {"user_id": key},
    )
    return UserOut.model_validate(row) if row else None


def update_user(user_id: UUID | str, data: dict[str, Any] | UserUpdate) -> Optional[UserOut]:
    """사용자 정보를 수정한다. 보낸 필드만 바꾸고 updated_at을 갱신한다.

    Args:
        user_id: 수정할 사용자 (users.id). UUID 또는 UUID 문자열.
        data: 바꿀 필드만 담은 dict 또는 user_schemas.UserUpdate. 예: {"name": "새 이름"}
            - 필드를 보내지 않으면 → 기존 값 유지
            - "phone_number": null → 번호 삭제
            - name / email은 null 불가
            - 빈 dict면 DB를 고치지 않고 현재 값을 그대로 돌려준다

    Returns:
        수정된 UserOut (patient_ids 포함). 해당 user_id가 없거나 id 형식이 틀리면 None.

    Raises:
        ValueError: 모르는 필드가 있거나 형식이 틀린 경우,
            바꾸려는 이메일·휴대폰 번호를 이미 다른 사용자가 쓰고 있는 경우.
    """
    fields = _to_model(UserUpdate, data).model_dump(exclude_unset=True)

    key = _parse_uuid(user_id)
    if key is None:
        return None
    if not fields:
        return get_user(key)

    try:
        row = _update_row("users", "id", key, fields)
    except UniqueViolation as error:
        raise ValueError(_unique_violation_message(error)) from error

    if row is None:
        return None

    logger.info("update_user: user_id=%s 수정 (%s)", key, ", ".join(fields))
    # patient_ids까지 담아 get_user()와 같은 형태로 돌려준다.
    return get_user(key)


def list_users(limit: int = 100, offset: int = 0) -> list[UserSummary]:
    """사용자 목록을 가입 순서대로 돌려준다. (개발·관리·배치용)

    테스트할 user_id 찾기, 전체 사용자를 도는 배치(예: 피드 일괄 생성)에 쓴다.
    ★ 전체 회원의 이름·이메일이 나오므로 API로 공개하지 않는다.

    Args:
        limit: 최대 반환 개수. 0 이하면 빈 목록.
        offset: 건너뛸 개수 (다음 페이지는 offset += limit).

    Returns:
        UserSummary 리스트, created_at → id 순 (같은 시각에 가입해도 순서가 고정된다).

    Raises:
        ValueError: offset이 음수인 경우.
    """
    if offset < 0:
        raise ValueError("offset은 0 이상이어야 합니다.")
    if limit <= 0:
        return []

    rows = _fetch_all(
        """
        SELECT u.id, u.name, u.email, u.created_at,
               (SELECT count(*) FROM patient_profiles AS p WHERE p.caregiver_id = u.id) AS patient_count
          FROM users AS u
         ORDER BY u.created_at, u.id
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"limit": limit, "offset": offset},
    )
    return [UserSummary.model_validate(row) for row in rows]


# ============================================================
# patient_profiles (환자)
# ============================================================

_PATIENT_COLUMNS = _columns(PatientOut)


def create_patient(patient: dict[str, Any] | PatientIn) -> PatientOut:
    """환자를 등록하고 저장된 행을 돌려준다.

    Args:
        patient: 환자 정보 (dict 또는 user_schemas.PatientIn).
            {"caregiver_id": "...", "name": "어머니", "dementia_stage": "경도",
             "diagnosis_date": "2025-03-02", "symptoms": ["수면장애", "배회"], "interests": ["운동"]}
            - caregiver_id: 필수 (users.id)
            - 나머지는 선택. 규칙은 PatientIn 참고

    Returns:
        저장된 PatientOut (DB가 만든 id, created_at, updated_at 포함).

    Raises:
        ValueError: 필드가 빠졌거나 형식이 틀린 경우, 존재하지 않는 간병인인 경우.
    """
    data = _to_model(PatientIn, patient)

    try:
        row = _insert_row("patient_profiles", data.model_dump())
    except ForeignKeyViolation as error:
        raise ValueError(f"존재하지 않는 사용자입니다: caregiver_id={data.caregiver_id}") from error

    logger.info("create_patient: patient_id=%s 저장 (caregiver_id=%s)", row["id"], data.caregiver_id)
    return PatientOut.model_validate(row)


def get_patient(patient_id: UUID | str) -> Optional[PatientOut]:
    """환자 한 명을 id로 조회한다. 없거나 id 형식이 틀리면 None."""
    key = _parse_uuid(patient_id)
    if key is None:
        return None
    row = _fetch_one(f"SELECT {_PATIENT_COLUMNS} FROM patient_profiles WHERE id = %(id)s", {"id": key})
    return PatientOut.model_validate(row) if row else None


def list_patients(caregiver_id: UUID | str) -> list[PatientOut]:
    """한 간병인이 담당하는 환자 목록을 등록 순서대로 돌려준다.

    get_user()의 patient_ids와 같은 순서다 (created_at → id).
    005의 idx_patient_profiles_caregiver (caregiver_id, created_at) 인덱스를 탄다.
    id 형식이 틀리거나 환자가 없으면 빈 목록.
    """
    key = _parse_uuid(caregiver_id)
    if key is None:
        return []
    rows = _fetch_all(
        f"""
        SELECT {_PATIENT_COLUMNS}
          FROM patient_profiles
         WHERE caregiver_id = %(caregiver_id)s
         ORDER BY created_at, id
        """,
        {"caregiver_id": key},
    )
    return [PatientOut.model_validate(row) for row in rows]


def update_patient(patient_id: UUID | str, data: dict[str, Any] | PatientUpdate) -> Optional[PatientOut]:
    """환자 정보를 수정한다. 보낸 필드만 바꾸고 updated_at을 갱신한다.

    Args:
        patient_id: 수정할 환자 (patient_profiles.id).
        data: 바꿀 필드만 담은 dict 또는 user_schemas.PatientUpdate. 예: {"dementia_stage": "중등도"}
            - caregiver_id는 바꿀 수 없다
            - name / dementia_stage / diagnosis_date를 null로 보내면 값 삭제
            - symptoms / interests는 목록 전체를 새 목록으로 바꾼다 (null이면 빈 목록)
            - 빈 dict면 DB를 고치지 않고 현재 값을 그대로 돌려준다

    Returns:
        수정된 PatientOut. 해당 환자가 없거나 id 형식이 틀리면 None.

    Raises:
        ValueError: 바꿀 수 없는 필드·모르는 필드가 있거나 형식이 틀린 경우.
    """
    fields = _to_model(PatientUpdate, data).model_dump(exclude_unset=True)

    key = _parse_uuid(patient_id)
    if key is None:
        return None
    if not fields:
        return get_patient(key)

    row = _update_row("patient_profiles", "id", key, fields)
    if row is None:
        return None

    logger.info("update_patient: patient_id=%s 수정 (%s)", key, ", ".join(fields))
    return PatientOut.model_validate(row)


def is_caregiver_of(user_id: UUID | str, patient_id: UUID | str) -> bool:
    """이 사용자가 이 환자의 담당 간병인인지 확인한다. (API 권한 확인용)

    환자 데이터를 읽거나 고치는 API는 이 함수로 먼저 확인한다.
    데이터 함수 자체는 권한을 보지 않는다 (AI 배치처럼 사용자 맥락이 없는 호출자도 있어서).
    id 형식이 틀리면 False.
    """
    user_key, patient_key = _parse_uuid(user_id), _parse_uuid(patient_id)
    if user_key is None or patient_key is None:
        return False
    row = _fetch_one(
        """
        SELECT EXISTS (
            SELECT 1 FROM patient_profiles
             WHERE id = %(patient_id)s AND caregiver_id = %(user_id)s
        ) AS ok
        """,
        {"patient_id": patient_key, "user_id": user_key},
    )
    return bool(row["ok"])


# ============================================================
# caregiver_profiles (간병인 자가점검)
# ============================================================

_CAREGIVER_COLUMNS = _columns(CaregiverProfileOut)


def upsert_caregiver_profile(
    user_id: UUID | str,
    data: dict[str, Any] | CaregiverProfileIn,
) -> Optional[CaregiverProfileOut]:
    """간병인 자가점검을 저장한다. 처음이면 만들고, 이미 있으면 보낸 필드만 바꾼다.

    사용자당 1행(user_id가 PK)이고, updated_at = 마지막 자가점검 시각이다.

    Args:
        user_id: 간병인 (users.id).
        data: 저장할 필드 (dict 또는 user_schemas.CaregiverProfileIn).
            예: {"relationship": "자녀", "burden_score": 32, "mood_score": 8}
            - 모두 선택. 보내지 않은 필드는 기존 값을 유지한다 (처음이면 비어 있음)
            - 점수 범위는 CaregiverProfileIn 참고 (burden 0~88, mood 0~27 정수)
            - 빈 dict면 저장하지 않고 현재 값(없으면 None)을 돌려준다

    Returns:
        저장된 CaregiverProfileOut. user_id 형식이 틀리면 None.

    Raises:
        ValueError: 모르는 필드가 있거나 형식·범위가 틀린 경우, 존재하지 않는 사용자인 경우.
    """
    fields = _to_model(CaregiverProfileIn, data).model_dump(exclude_unset=True)

    key = _parse_uuid(user_id)
    if key is None:
        return None
    if not fields:
        return get_caregiver_profile(key)

    try:
        row = _upsert_row("caregiver_profiles", ["user_id"], {"user_id": key, **fields}, list(fields))
    except ForeignKeyViolation as error:
        raise ValueError(f"존재하지 않는 사용자입니다: user_id={key}") from error

    logger.info("upsert_caregiver_profile: user_id=%s 저장 (%s)", key, ", ".join(fields))
    return CaregiverProfileOut.model_validate(row)


def get_caregiver_profile(user_id: UUID | str) -> Optional[CaregiverProfileOut]:
    """간병인 자가점검을 조회한다. 아직 저장한 적이 없거나 id 형식이 틀리면 None."""
    key = _parse_uuid(user_id)
    if key is None:
        return None
    row = _fetch_one(
        f"SELECT {_CAREGIVER_COLUMNS} FROM caregiver_profiles WHERE user_id = %(user_id)s",
        {"user_id": key},
    )
    return CaregiverProfileOut.model_validate(row) if row else None


# ============================================================
# clinical_assessments (치매 평가 이력)
# ============================================================
# 이 테이블에는 updated_at 컬럼이 없다 (004 스키마).

_ASSESSMENT_COLUMNS = _columns(AssessmentOut)


def list_assessment_types() -> list[AssessmentTypeInfo]:
    """지원하는 검사 목록과 점수 규칙을 돌려준다. (DB에 가지 않는다)

    앱의 검사 선택 목록·점수 입력 제한, AI의 점수 해석(높을수록 나쁜지)에 쓴다.
    규칙의 정본은 user_schemas.ASSESSMENT_TYPES이고, create/update의 검증 기준과 같다.
    """
    infos = []
    for name, spec in ASSESSMENT_TYPES.items():
        allowed = spec.get("allowed")
        infos.append(
            AssessmentTypeInfo(
                assessment_type=name,
                name=spec["name"],
                min=min(allowed) if allowed else spec["min"],
                max=max(allowed) if allowed else spec["max"],
                step=spec.get("step"),
                allowed=allowed,
                higher_is_worse=spec["higher_is_worse"],
            )
        )
    return infos


def create_assessment(assessment: dict[str, Any] | AssessmentIn) -> AssessmentOut:
    """치매 평가 결과 1건을 저장하고 저장된 행을 돌려준다.

    검사명은 표준 이름으로 바꿔 저장하고, 점수는 검사별 범위·단위에 맞는지 검사한다
    (규칙은 user_schemas.ASSESSMENT_TYPES, 목록은 list_assessment_types()로도 볼 수 있다).

    Args:
        assessment: 평가 결과 (dict 또는 user_schemas.AssessmentIn).
            {"patient_id": "...", "assessment_type": "K-MMSE", "score": 24,
             "assessed_at": "2026-10-01", "result_detail": "...", "assessed_by": "OO병원"}
            - patient_id, assessment_type, assessed_at: 필수 (assessed_at은 미래 날짜 불가)
            - score: 선택 (점수 없이 판정만 기록할 때는 비운다)

    Returns:
        저장된 AssessmentOut (DB가 만든 id, created_at 포함). score는 float.

    Raises:
        ValueError: 필드가 빠졌거나 형식이 틀린 경우, 지원하지 않는 검사명,
            점수가 검사 범위·단위에 맞지 않는 경우, 존재하지 않는 환자인 경우.
    """
    data = _to_model(AssessmentIn, assessment)

    try:
        row = _insert_row("clinical_assessments", data.model_dump())
    except ForeignKeyViolation as error:
        raise ValueError(f"존재하지 않는 환자입니다: patient_id={data.patient_id}") from error

    logger.info(
        "create_assessment: assessment_id=%s 저장 (patient_id=%s, %s)",
        row["id"], data.patient_id, data.assessment_type,
    )
    return AssessmentOut.model_validate(row)


def get_assessment(assessment_id: UUID | str) -> Optional[AssessmentOut]:
    """평가 기록 1건을 id로 조회한다. 없거나 id 형식이 틀리면 None."""
    key = _parse_uuid(assessment_id)
    if key is None:
        return None
    row = _fetch_one(f"SELECT {_ASSESSMENT_COLUMNS} FROM clinical_assessments WHERE id = %(id)s", {"id": key})
    return AssessmentOut.model_validate(row) if row else None


def list_assessments(
    patient_id: UUID | str,
    assessment_type: Optional[str] = None,
    limit: int = 100,
) -> list[AssessmentOut]:
    """한 환자의 평가 이력을 최근 것부터 돌려준다. (척도별 추이 그래프·기록 화면용)

    Args:
        patient_id: 환자 (patient_profiles.id). 형식이 틀리면 빈 목록.
        assessment_type: 검사명 필터 (별칭도 받는다, 예: 'k-mmse'). None이면 전체.
        limit: 최대 반환 개수. 0 이하면 빈 목록.

    Returns:
        AssessmentOut 리스트. assessed_at DESC → created_at DESC → id 순
        (같은 날 같은 검사를 두 번 넣어도 순서가 고정된다).
        idx_clinical_assessments_patient (patient_id, assessment_type, assessed_at DESC) 인덱스를 탄다.

    Raises:
        ValueError: assessment_type이 지원하지 않는 검사명인 경우 (오타를 빈 결과로 숨기지 않는다).
    """
    canonical = normalize_assessment_type(assessment_type) if assessment_type is not None else None
    key = _parse_uuid(patient_id)
    if key is None or limit <= 0:
        return []

    rows = _fetch_all(
        f"""
        SELECT {_ASSESSMENT_COLUMNS}
          FROM clinical_assessments
         WHERE patient_id = %(patient_id)s
           AND (%(assessment_type)s::text IS NULL OR assessment_type = %(assessment_type)s::text)
         ORDER BY assessed_at DESC, created_at DESC, id
         LIMIT %(limit)s
        """,
        {"patient_id": key, "assessment_type": canonical, "limit": limit},
    )
    return [AssessmentOut.model_validate(row) for row in rows]


def get_latest_assessments(patient_id: UUID | str) -> list[AssessmentOut]:
    """한 환자의 검사별 가장 최근 평가를 1건씩 돌려준다. (현재 상태 요약·개인화·AI용)

    예: K-MMSE를 3번, CDR을 2번 받았으면 K-MMSE 최신 1건 + CDR 최신 1건 = 2건.

    Returns:
        AssessmentOut 리스트, assessment_type 이름순. 기록이 없거나 id 형식이 틀리면 빈 목록.
        같은 날 같은 검사가 여러 건이면 나중에 입력한 것(created_at)이 최신이다.
    """
    key = _parse_uuid(patient_id)
    if key is None:
        return []

    # DISTINCT ON (assessment_type): 검사별로 ORDER BY 순서상 첫 행만 남긴다.
    rows = _fetch_all(
        f"""
        SELECT DISTINCT ON (assessment_type) {_ASSESSMENT_COLUMNS}
          FROM clinical_assessments
         WHERE patient_id = %(patient_id)s
         ORDER BY assessment_type, assessed_at DESC, created_at DESC, id
        """,
        {"patient_id": key},
    )
    return [AssessmentOut.model_validate(row) for row in rows]


def update_assessment(
    assessment_id: UUID | str,
    data: dict[str, Any] | AssessmentUpdate,
) -> Optional[AssessmentOut]:
    """평가 기록을 수정한다 (입력 실수 정정용). 보낸 필드만 바꾼다.

    점수 검사는 "수정 후의 검사명 + 수정 후의 점수" 조합으로 한다 (DB의 현재 값과 합쳐서).
      - 점수만 바꾸면: 기존 검사명 기준으로 범위 검사
      - 검사명만 바꾸면: 기존 점수가 새 검사 범위에 맞는지 검사
        (예: K-MMSE 24점 기록을 CDR로 바꾸면 거부 — 점수도 함께 고쳐야 한다)

    Args:
        assessment_id: 수정할 평가 기록 (clinical_assessments.id).
        data: 바꿀 필드만 담은 dict 또는 user_schemas.AssessmentUpdate. 예: {"score": 23}
            - patient_id는 바꿀 수 없다 (다른 환자로 옮기는 수정은 거부)
            - "score": null → 점수 삭제, assessment_type / assessed_at은 null 불가
            - 빈 dict면 DB를 고치지 않고 현재 값을 그대로 돌려준다

    Returns:
        수정된 AssessmentOut. 해당 id가 없거나 id 형식이 틀리면 None.

    Raises:
        ValueError: 바꿀 수 없는 필드·모르는 필드가 있거나 형식이 틀린 경우,
            수정 후 점수가 검사 범위·단위에 맞지 않는 경우.
    """
    fields = _to_model(AssessmentUpdate, data).model_dump(exclude_unset=True)

    key = _parse_uuid(assessment_id)
    if key is None:
        return None
    current = get_assessment(key)
    if current is None:
        return None
    if not fields:
        return current

    # 검사명이나 점수를 바꿀 때만 수정 후 조합으로 점수 범위를 다시 검사한다
    # (메모·날짜만 고칠 때는 기존 값을 건드리지 않는다). 점수를 보냈으면 검사된 Decimal로 저장한다.
    if "assessment_type" in fields or "score" in fields:
        new_type = fields.get("assessment_type", current.assessment_type)
        new_score = fields["score"] if "score" in fields else current.score
        checked_score = check_assessment_score(new_type, new_score)
        if "score" in fields:
            fields["score"] = checked_score

    row = _update_row("clinical_assessments", "id", key, fields, touch_updated_at=False)
    if row is None:  # 조회와 수정 사이에 삭제된 경우
        return None

    logger.info("update_assessment: assessment_id=%s 수정 (%s)", key, ", ".join(fields))
    return AssessmentOut.model_validate(row)


# ============================================================
# safety_events (안전·행동 이벤트)
# ============================================================
# 이 테이블에는 updated_at 컬럼이 없다 (004 스키마).

_SAFETY_COLUMNS = _columns(SafetyEventOut)
_SAFETY_KEY_COLUMNS = ("patient_id", "event_date")


def upsert_safety_event(event: dict[str, Any] | SafetyEventIn) -> SafetyEventOut:
    """하루치 안전·행동 기록을 저장한다. 그날 기록이 없으면 만들고, 있으면 보낸 필드만 바꾼다.

    "오늘의 체크" 화면처럼 같은 날 여러 번 저장해도 하루 1행이 유지된다.
    예: 오전에 {"has_fall": true}, 저녁에 {"has_wandering": true} → 두 값 모두 true로 남는다.
    patient_id + event_date만 보내면 "이상 없음" 기록이 된다 (이미 있으면 그대로 둔다).

    Args:
        event: 기록 (dict 또는 user_schemas.SafetyEventIn).
            {"patient_id": "...", "event_date": "2026-10-07", "has_wandering": true,
             "note": "새벽 3시 현관 밖에서 발견"}
            - patient_id, event_date: 필수 (event_date는 미래 날짜 불가)
            - has_fall / has_wandering / has_missing: 선택, true/false만
            - note: 선택. null을 보내면 메모 삭제

    Returns:
        저장된 그날의 SafetyEventOut.

    Raises:
        ValueError: 필드가 빠졌거나 형식이 틀린 경우, 존재하지 않는 환자인 경우.
    """
    data = _to_model(SafetyEventIn, event)
    # 새 행이면 전체 값(보내지 않은 플래그는 기본값 false)으로 넣고,
    # 이미 있으면 실제로 보낸 필드만 바꾼다 (patient_id / event_date는 충돌 키라 제외).
    update_columns = [name for name in data.model_fields_set if name not in _SAFETY_KEY_COLUMNS]

    try:
        row = _upsert_row(
            "safety_events",
            list(_SAFETY_KEY_COLUMNS),
            data.model_dump(),
            sorted(update_columns),
            touch_updated_at=False,
        )
    except ForeignKeyViolation as error:
        raise ValueError(f"존재하지 않는 환자입니다: patient_id={data.patient_id}") from error

    logger.info("upsert_safety_event: patient_id=%s %s 저장", data.patient_id, data.event_date)
    return SafetyEventOut.model_validate(row)


def get_safety_event(event_id: UUID | str) -> Optional[SafetyEventOut]:
    """안전·행동 기록 1건을 id로 조회한다. 없거나 id 형식이 틀리면 None."""
    key = _parse_uuid(event_id)
    if key is None:
        return None
    row = _fetch_one(f"SELECT {_SAFETY_COLUMNS} FROM safety_events WHERE id = %(id)s", {"id": key})
    return SafetyEventOut.model_validate(row) if row else None


def list_safety_events(
    patient_id: UUID | str,
    start_date: Optional[str | date] = None,
    end_date: Optional[str | date] = None,
    limit: int = 100,
) -> list[SafetyEventOut]:
    """한 환자의 안전·행동 기록을 최근 날짜부터 돌려준다.

    오늘 기록만 보려면 start_date = end_date = 오늘. 최근 30일은 start_date = 30일 전.

    Args:
        patient_id: 환자 (patient_profiles.id). 형식이 틀리면 빈 목록.
        start_date / end_date: 'YYYY-MM-DD' (양 끝 포함). None이면 그쪽 제한 없음.
        limit: 최대 반환 개수. 0 이하면 빈 목록.

    Returns:
        SafetyEventOut 리스트, event_date DESC (하루 1행이라 날짜만으로 순서가 고정된다).
        UNIQUE (patient_id, event_date) 인덱스를 탄다.

    Raises:
        ValueError: 날짜 형식이 틀렸거나 end_date가 start_date보다 빠른 경우.
    """
    start = parse_optional_date(start_date, "start_date", allow_future=True)
    end = parse_optional_date(end_date, "end_date", allow_future=True)
    check_date_order(start, end, "start_date", "end_date")
    key = _parse_uuid(patient_id)
    if key is None or limit <= 0:
        return []

    rows = _fetch_all(
        f"""
        SELECT {_SAFETY_COLUMNS}
          FROM safety_events
         WHERE patient_id = %(patient_id)s
           AND (%(start)s::date IS NULL OR event_date >= %(start)s::date)
           AND (%(end)s::date IS NULL OR event_date <= %(end)s::date)
         ORDER BY event_date DESC
         LIMIT %(limit)s
        """,
        {"patient_id": key, "start": start, "end": end, "limit": limit},
    )
    return [SafetyEventOut.model_validate(row) for row in rows]


# ============================================================
# medications (복약 정보)
# ============================================================

_MEDICATION_COLUMNS = _columns(MedicationOut)


def create_medication(medication: dict[str, Any] | MedicationIn) -> MedicationOut:
    """복약 정보 1건을 저장하고 저장된 행을 돌려준다.

    Args:
        medication: 복약 정보 (dict 또는 user_schemas.MedicationIn).
            {"patient_id": "...", "drug_name": "도네페질 5mg", "dosage": "1정",
             "frequency": "1일 1회 취침 전", "start_date": "2026-10-01"}
            - patient_id, drug_name: 필수
            - is_taking: 선택, 기본 true (현재 복용 중)
            - start_date / end_date: 선택, 미래 가능. end_date ≥ start_date

    Returns:
        저장된 MedicationOut (DB가 만든 id, created_at, updated_at 포함).

    Raises:
        ValueError: 필드가 빠졌거나 형식이 틀린 경우, 종료일이 시작일보다 빠른 경우,
            존재하지 않는 환자인 경우.
    """
    data = _to_model(MedicationIn, medication)

    try:
        row = _insert_row("medications", data.model_dump())
    except ForeignKeyViolation as error:
        raise ValueError(f"존재하지 않는 환자입니다: patient_id={data.patient_id}") from error

    logger.info("create_medication: medication_id=%s 저장 (patient_id=%s)", row["id"], data.patient_id)
    return MedicationOut.model_validate(row)


def get_medication(medication_id: UUID | str) -> Optional[MedicationOut]:
    """복약 정보 1건을 id로 조회한다. 없거나 id 형식이 틀리면 None."""
    key = _parse_uuid(medication_id)
    if key is None:
        return None
    row = _fetch_one(f"SELECT {_MEDICATION_COLUMNS} FROM medications WHERE id = %(id)s", {"id": key})
    return MedicationOut.model_validate(row) if row else None


def list_medications(patient_id: UUID | str, is_taking: Optional[bool] = None) -> list[MedicationOut]:
    """한 환자의 복약 목록을 돌려준다.

    Args:
        patient_id: 환자 (patient_profiles.id). 형식이 틀리면 빈 목록.
        is_taking: True면 현재 복용 중인 약만, False면 중단한 약만, None이면 전체.

    Returns:
        MedicationOut 리스트. 복용 중인 약 먼저 → 시작일 최근 순(없으면 뒤로) → 등록 최근 순 → id.
        idx_medications_patient (patient_id, is_taking) 인덱스를 탄다.

    Raises:
        ValueError: is_taking이 true/false/None이 아닌 경우.
    """
    taking = _check_flag(is_taking, "is_taking")
    key = _parse_uuid(patient_id)
    if key is None:
        return []

    rows = _fetch_all(
        f"""
        SELECT {_MEDICATION_COLUMNS}
          FROM medications
         WHERE patient_id = %(patient_id)s
           AND (%(is_taking)s::boolean IS NULL OR is_taking = %(is_taking)s::boolean)
         ORDER BY is_taking DESC, start_date DESC NULLS LAST, created_at DESC, id
        """,
        {"patient_id": key, "is_taking": taking},
    )
    return [MedicationOut.model_validate(row) for row in rows]


def update_medication(
    medication_id: UUID | str,
    data: dict[str, Any] | MedicationUpdate,
) -> Optional[MedicationOut]:
    """복약 정보를 수정한다. 보낸 필드만 바꾸고 updated_at을 갱신한다.

    복용 중단은 {"is_taking": false, "end_date": "2026-10-07"}로 한다 (행을 지우지 않는다).
    날짜 순서는 "수정 후의 시작일 + 수정 후의 종료일" 조합으로 검사한다
    (종료일만 바꿔도 기존 시작일보다 빠르면 거부).

    Args:
        medication_id: 수정할 복약 기록 (medications.id).
        data: 바꿀 필드만 담은 dict 또는 user_schemas.MedicationUpdate.
            - patient_id는 바꿀 수 없다, drug_name / is_taking은 null 불가
            - 다른 텍스트·날짜는 null이면 값 삭제
            - 빈 dict면 DB를 고치지 않고 현재 값을 그대로 돌려준다

    Returns:
        수정된 MedicationOut. 해당 id가 없거나 id 형식이 틀리면 None.

    Raises:
        ValueError: 바꿀 수 없는 필드·모르는 필드가 있거나 형식이 틀린 경우,
            수정 후 종료일이 시작일보다 빠른 경우.
    """
    fields = _to_model(MedicationUpdate, data).model_dump(exclude_unset=True)

    key = _parse_uuid(medication_id)
    if key is None:
        return None
    current = get_medication(key)
    if current is None:
        return None
    if not fields:
        return current

    merged = {**current.model_dump(), **fields}
    check_date_order(merged["start_date"], merged["end_date"], "start_date", "end_date")

    row = _update_row("medications", "id", key, fields)
    if row is None:  # 조회와 수정 사이에 삭제된 경우
        return None

    logger.info("update_medication: medication_id=%s 수정 (%s)", key, ", ".join(fields))
    return MedicationOut.model_validate(row)


# ============================================================
# medical_visits (병원 방문 기록)
# ============================================================

_VISIT_COLUMNS = _columns(MedicalVisitOut)


def create_medical_visit(visit: dict[str, Any] | MedicalVisitIn) -> MedicalVisitOut:
    """병원 방문(또는 예약) 1건을 저장하고 저장된 행을 돌려준다.

    Args:
        visit: 방문 기록 (dict 또는 user_schemas.MedicalVisitIn).
            예약: {"patient_id": "...", "visit_date": "2026-11-02", "hospital_name": "OO병원",
                   "department": "신경과", "visit_reason": "정기 진료"}
            방문 후: 위 + {"is_visited": true, "diagnosis": "...", "medication_change": "...", ...}
            - patient_id, visit_date: 필수 (예약이면 미래 가능)
            - is_visited: 선택, 기본 false. 미래 날짜는 true 불가

    Returns:
        저장된 MedicalVisitOut (DB가 만든 id, created_at, updated_at 포함).

    Raises:
        ValueError: 필드가 빠졌거나 형식이 틀린 경우, 미래 날짜를 방문 완료로 넣은 경우,
            존재하지 않는 환자인 경우.
    """
    data = _to_model(MedicalVisitIn, visit)

    try:
        row = _insert_row("medical_visits", data.model_dump())
    except ForeignKeyViolation as error:
        raise ValueError(f"존재하지 않는 환자입니다: patient_id={data.patient_id}") from error

    logger.info("create_medical_visit: visit_id=%s 저장 (patient_id=%s)", row["id"], data.patient_id)
    return MedicalVisitOut.model_validate(row)


def get_medical_visit(visit_id: UUID | str) -> Optional[MedicalVisitOut]:
    """방문 기록 1건을 id로 조회한다. 없거나 id 형식이 틀리면 None."""
    key = _parse_uuid(visit_id)
    if key is None:
        return None
    row = _fetch_one(f"SELECT {_VISIT_COLUMNS} FROM medical_visits WHERE id = %(id)s", {"id": key})
    return MedicalVisitOut.model_validate(row) if row else None


def list_medical_visits(
    patient_id: UUID | str,
    is_visited: Optional[bool] = None,
    start_date: Optional[str | date] = None,
    end_date: Optional[str | date] = None,
    limit: int = 100,
) -> list[MedicalVisitOut]:
    """한 환자의 병원 방문 기록을 최근 날짜부터 돌려준다.

    다가오는 예약: is_visited=False, start_date=오늘 / 지난 진료 이력: is_visited=True

    Args:
        patient_id: 환자 (patient_profiles.id). 형식이 틀리면 빈 목록.
        is_visited: True면 다녀온 진료만, False면 예약(미방문)만, None이면 전체.
        start_date / end_date: 'YYYY-MM-DD' (양 끝 포함). None이면 그쪽 제한 없음.
        limit: 최대 반환 개수. 0 이하면 빈 목록.

    Returns:
        MedicalVisitOut 리스트, visit_date DESC → created_at DESC → id.
        idx_medical_visits_patient_date (patient_id, visit_date DESC) 인덱스를 탄다.

    Raises:
        ValueError: is_visited가 true/false/None이 아니거나, 날짜 형식이 틀렸거나,
            end_date가 start_date보다 빠른 경우.
    """
    visited = _check_flag(is_visited, "is_visited")
    start = parse_optional_date(start_date, "start_date", allow_future=True)
    end = parse_optional_date(end_date, "end_date", allow_future=True)
    check_date_order(start, end, "start_date", "end_date")
    key = _parse_uuid(patient_id)
    if key is None or limit <= 0:
        return []

    rows = _fetch_all(
        f"""
        SELECT {_VISIT_COLUMNS}
          FROM medical_visits
         WHERE patient_id = %(patient_id)s
           AND (%(is_visited)s::boolean IS NULL OR is_visited = %(is_visited)s::boolean)
           AND (%(start)s::date IS NULL OR visit_date >= %(start)s::date)
           AND (%(end)s::date IS NULL OR visit_date <= %(end)s::date)
         ORDER BY visit_date DESC, created_at DESC, id
         LIMIT %(limit)s
        """,
        {"patient_id": key, "is_visited": visited, "start": start, "end": end, "limit": limit},
    )
    return [MedicalVisitOut.model_validate(row) for row in rows]


def update_medical_visit(
    visit_id: UUID | str,
    data: dict[str, Any] | MedicalVisitUpdate,
) -> Optional[MedicalVisitOut]:
    """방문 기록을 수정한다. 보낸 필드만 바꾸고 updated_at을 갱신한다.

    다녀온 뒤에는 {"is_visited": true, "diagnosis": "...", "medication_change": "...", ...}로 채운다.
    "미래 날짜인데 방문 완료"인지는 수정 후의 visit_date + is_visited 조합으로 검사한다.

    Args:
        visit_id: 수정할 방문 기록 (medical_visits.id).
        data: 바꿀 필드만 담은 dict 또는 user_schemas.MedicalVisitUpdate.
            - patient_id는 바꿀 수 없다, visit_date / is_visited는 null 불가
            - 진료 항목 텍스트는 null이면 값 삭제
            - 빈 dict면 DB를 고치지 않고 현재 값을 그대로 돌려준다

    Returns:
        수정된 MedicalVisitOut. 해당 id가 없거나 id 형식이 틀리면 None.

    Raises:
        ValueError: 바꿀 수 없는 필드·모르는 필드가 있거나 형식이 틀린 경우,
            수정 후 미래 날짜인데 방문 완료인 경우.
    """
    fields = _to_model(MedicalVisitUpdate, data).model_dump(exclude_unset=True)

    key = _parse_uuid(visit_id)
    if key is None:
        return None
    current = get_medical_visit(key)
    if current is None:
        return None
    if not fields:
        return current

    merged = {**current.model_dump(), **fields}
    check_visit_state(merged["visit_date"], merged["is_visited"])

    row = _update_row("medical_visits", "id", key, fields)
    if row is None:  # 조회와 수정 사이에 삭제된 경우
        return None

    logger.info("update_medical_visit: visit_id=%s 수정 (%s)", key, ", ".join(fields))
    return MedicalVisitOut.model_validate(row)


# ============================================================
# 프로필 묶음 (AI 피드·챗봇 입력용)
# ============================================================


def _patient_context(patient: PatientOut, now: date) -> PatientContext:
    """환자 한 명의 현재 상태를 위의 조회 함수들로 모은다."""
    events = list_safety_events(patient.id, start_date=now - timedelta(days=PROFILE_SAFETY_DAYS - 1), end_date=now)
    # list_medical_visits는 최근 날짜부터 주므로, 예약은 뒤집어 가까운 날짜부터 보여준다.
    upcoming = list_medical_visits(patient.id, is_visited=False, start_date=now)
    return PatientContext(
        patient=patient,
        latest_assessments=get_latest_assessments(patient.id),
        current_medications=list_medications(patient.id, is_taking=True),
        recent_safety_events=[e for e in events if e.has_fall or e.has_wandering or e.has_missing],
        recent_visits=list_medical_visits(patient.id, is_visited=True, limit=PROFILE_RECENT_VISITS),
        upcoming_visits=upcoming[::-1],
    )


def get_caregiver_context(user_id: UUID | str) -> Optional[CaregiverContext]:
    """간병인 정보만 돌려준다 — 이름 + 자가점검 + 담당 환자 id 목록 (환자 상세 없음).

    환자 상세는 patient_ids로 get_patient_context()를 부른다. email / phone_number는 담지 않는다.

    Returns:
        CaregiverContext. 해당 user_id가 없거나 id 형식이 틀리면 None.
    """
    user = get_user(user_id)
    if user is None:
        return None
    return CaregiverContext(
        user_id=user.id,
        name=user.name,
        caregiver_profile=get_caregiver_profile(user.id),
        patient_ids=user.patient_ids,
        generated_at=datetime.now(APP_TIMEZONE),
    )


def get_patient_context(patient_id: UUID | str) -> Optional[PatientContext]:
    """환자 한 명의 현재 상태 묶음을 돌려준다. get_user_profile_context()의 patients 한 항목과 같은 형식.

    권한은 보지 않는다 (AI 배치처럼 사용자 맥락이 없는 호출자도 쓴다).
    앱 API는 is_caregiver_of()로 이 사용자의 환자인지 먼저 확인한다.

    Returns:
        PatientContext. 해당 환자가 없거나 id 형식이 틀리면 None.
    """
    patient = get_patient(patient_id)
    if patient is None:
        return None
    return _patient_context(patient, today())


def get_user_profile_context(user_id: UUID | str) -> Optional[UserProfileContext]:
    """사용자 한 명의 프로필 전체(간병인 + 담당 환자들의 현재 상태)를 한 번에 돌려준다.

    AI 피드·챗봇이 개인화 입력으로 쓰는 형식이다. AI 쪽은 이 함수 결과만 받으면 되고,
    아래 조회 함수들을 직접 조합할 필요가 없다. JSON 텍스트는 `.model_dump_json()`.

    담는 내용 (기간·개수 기준은 user_schemas의 PROFILE_* 상수, 날짜는 한국 시간 기준):
        - 사용자 이름 + 간병인 자가점검 (email / phone_number는 담지 않는다)
        - 환자마다: 기본 정보, 검사별 최신 평가, 복용 중인 약,
          최근 PROFILE_SAFETY_DAYS일 중 낙상·배회·실종이 있던 날, 다녀온 진료 최근 PROFILE_RECENT_VISITS건,
          오늘 이후 진료 예약

    권한은 보지 않는다 (다른 함수와 같음). 사용자 본인 요청인지는 API가 확인한다.

    Args:
        user_id: 조회할 사용자 (users.id). UUID 또는 UUID 문자열.

    Returns:
        UserProfileContext. 해당 user_id가 없거나 id 형식이 틀리면 None.
    """
    user = get_user(user_id)
    if user is None:
        return None

    now = today()
    return UserProfileContext(
        user_id=user.id,
        name=user.name,
        caregiver_profile=get_caregiver_profile(user.id),
        patients=[_patient_context(patient, now) for patient in list_patients(user.id)],
        generated_at=datetime.now(APP_TIMEZONE),
    )
