"""기존 간병인에 로그인 테스트용 인증 정보를 연결한다 (development/test 전용).

backend/에서 실행:
    # 최초 1회, 011이 아직 적용되지 않은 DB에 적용
    docker exec -i mindcare-db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 --single-transaction < app/db/migrations/011_authentication.sql
    python -m scripts.seed_dev_auth --all-dev-profiles --dry-run
    python -m scripts.seed_dev_auth --all-dev-profiles
    python -m scripts.seed_dev_auth --email caregiver@example.com

비밀번호는 숨김 프롬프트로 입력한다. 자동화에서는 MINDCARE_TEST_PASSWORD 환경 변수를
사용한다. 비밀번호를 명령행 인자·출력·파일에 남기지 않는다. 이 테스트 도구는 12~128자만
받으며 공백이나 대소문자를 바꾸지 않는다 (향후 가입 API의 정책을 확정하는 코드는 아니다).

새 사용자를 만들지 않고 users.id에 user_credentials.user_id를 연결한다.
환자·자가점검·기록은 그대로 유지한다. 같은 비밀번호로 재실행하면 기존 인증 정보를
검증만 하며, 다른 비밀번호나 비활성 계정이 있으면 전체 작업을 롤백한다.
--dry-run은 실제 INSERT와 검증을 수행한 뒤 롤백한다. 세션이나 JWT는 발급하지 않는다.

이 파일의 SQL·verify_test_login은 테스트 데이터 준비·점검 전용이다.
실제 로그인 API에는 인증 서비스·DB 접근 함수·요청 제한 등을 별도로 구현해야 한다.
"""
import argparse
from dataclasses import dataclass
from getpass import getpass
import os
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
import psycopg

from app.config import settings
from app.db.connection import get_connection
from app.user_schemas import normalize_email


PASSWORD_HASHER = PasswordHasher()
SEED_EMAIL_LIKE = "dev-seed-%@example.com"


@dataclass(frozen=True)
class SeedResult:
    user_id: UUID
    email: str
    patient_count: int
    created: bool


def verify_test_login(conn: psycopg.Connection, email: str, password: str) -> UUID | None:
    """테스트용 이메일·비밀번호 검증. 성공하면 기존 간병인의 users.id를 반환한다."""
    row = conn.execute(
        """SELECT u.id, c.password_hash, c.is_active
             FROM users u JOIN user_credentials c ON c.user_id = u.id
            WHERE lower(btrim(u.email)) = %s""",
        (normalize_email(email),),
    ).fetchone()
    if row is None or not row[2]:
        return None
    try:
        PASSWORD_HASHER.verify(row[1], password)
    except (VerificationError, InvalidHashError):
        return None
    return row[0]


def seed_credentials(
    conn: psycopg.Connection,
    emails: list[str],
    password: str,
    *,
    dry_run: bool = False,
) -> list[SeedResult]:
    """지정한 기존 간병인에만 인증 정보를 추가한다. 실패 시 이번 변경 전체를 롤백한다."""
    if not 12 <= len(password) <= 128:
        raise ValueError("테스트 비밀번호는 12~128자로 입력하세요.")
    targets = sorted({normalize_email(email) for email in emails})
    if not targets:
        raise ValueError("대상 간병인이 없습니다. 먼저 seed_dev_profiles를 실행하거나 --email을 지정하세요.")

    results = []
    with conn.transaction(force_rollback=dry_run):
        if conn.execute("SELECT to_regclass('user_credentials')").fetchone()[0] is None:
            raise ValueError("인증 테이블이 없습니다. 먼저 011_authentication.sql을 적용하세요.")
        for email in targets:
            # 같은 간병인에 대한 동시 시드 실행을 직렬화한다.
            rows = conn.execute(
                "SELECT id, email FROM users WHERE lower(btrim(email)) = %s FOR UPDATE",
                (email,),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError(f"이메일에 해당하는 기존 간병인이 정확히 1명이어야 합니다: {email}")
            user_id, stored_email = rows[0]
            existing = conn.execute(
                "SELECT user_id FROM user_credentials WHERE user_id = %s FOR UPDATE", (user_id,),
            ).fetchone()
            created = existing is None
            if created:
                # 계정마다 새 salt를 사용한다. 기존 해시·활성 상태를 덮어쓰지 않는다.
                conn.execute(
                    "INSERT INTO user_credentials(user_id, password_hash) VALUES (%s, %s)",
                    (user_id, PASSWORD_HASHER.hash(password)),
                )
            if verify_test_login(conn, email, password) != user_id:
                raise ValueError(
                    f"기존 인증 정보로 검증되지 않습니다 (비밀번호 불일치·비활성·잘못된 해시): {email}. "
                    "인증 정보는 덮어쓰지 않았습니다."
                )
            patient_count = conn.execute(
                "SELECT count(*) FROM patient_profiles WHERE caregiver_id = %s", (user_id,),
            ).fetchone()[0]
            results.append(SeedResult(user_id, stored_email, patient_count, created))
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="기존 간병인에 로그인 테스트용 비밀번호 연결")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--email", action="append", help="기존 간병인 이메일 (여러 번 지정 가능)")
    target.add_argument("--all-dev-profiles", action="store_true", help="dev-seed-*@example.com 예시 간병인 전체")
    parser.add_argument("--dry-run", action="store_true", help="인증 정보 저장·검증 후 롤백")
    args = parser.parse_args(argv)

    if settings.app_env not in {"development", "test"}:
        parser.error("이 스크립트는 APP_ENV=development 또는 test에서만 실행할 수 있습니다.")
    try:
        password = os.environ.get("MINDCARE_TEST_PASSWORD")
        if password is None:
            password = getpass("테스트 로그인 비밀번호 (12~128자): ")
            if password != getpass("비밀번호 확인: "):
                raise ValueError("두 비밀번호가 일치하지 않습니다.")
        with get_connection() as conn:
            emails = args.email
            if args.all_dev_profiles:
                emails = [row[0] for row in conn.execute(
                    "SELECT email FROM users WHERE email LIKE %s ORDER BY email", (SEED_EMAIL_LIKE,),
                ).fetchall()]
            results = seed_credentials(conn, emails, password, dry_run=args.dry_run)
    except (ValueError, EOFError) as error:
        raise SystemExit(str(error)) from None
    except psycopg.Error:
        # DB 오류의 DETAIL에 비밀번호 해시 등 입력값이 포함될 수 있어 그대로 출력하지 않는다.
        raise SystemExit("DB 작업에 실패했습니다. 연결 설정과 011 적용 상태를 확인하세요. 이번 변경은 롤백됐습니다.") from None

    for result in results:
        status = "신규 연결" if result.created else "기존 인증 유지"
        print(f"[{status}] {result.email} | user_id={result.user_id} | 담당 환자 {result.patient_count}명 | 비밀번호 검증 성공")
    print("검증 완료: --dry-run으로 이번 변경을 롤백했습니다." if args.dry_run else "검증 완료: 로그인 테스트용 인증 정보를 저장했습니다.")
    print("로그인 API·JWT 발급은 별도 구현이 필요합니다.")


if __name__ == "__main__":
    main()
