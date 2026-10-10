"""DB에 있는 사용자(간병인) 목록과 담당 환자를 표로 출력한다. (테스트할 user_id / patient_id 찾기용)

user_functions.list_users() + list_patients()로만 읽는다 (직접 SQL 없음). 이름은 중복될 수 있으니
사람을 구분할 때는 이메일을 본다.

실행 (backend/ 에서, 로컬 DB를 띄운 상태로):
    python -m scripts.list_users
    python -m scripts.list_users --limit 20 --offset 20   # 다음 페이지
"""
import argparse
import unicodedata

import psycopg

from app.db import user_functions as uf


def width(text: str) -> int:
    """터미널 출력 폭. 한글 등 전각 문자는 2칸으로 센다 (표 칸 맞추기용)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, size: int) -> str:
    return text + " " * max(size - width(text), 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="사용자 목록 출력")
    parser.add_argument("--limit", type=int, default=100, help="최대 출력 수 (기본 100)")
    parser.add_argument("--offset", type=int, default=0, help="건너뛸 수 (기본 0)")
    args = parser.parse_args()

    try:
        users = uf.list_users(limit=args.limit, offset=args.offset)
    except psycopg.OperationalError as error:
        raise SystemExit(
            "DB에 연결할 수 없습니다. backend/에서 `docker compose up -d`로 DB를 띄웠는지, "
            f".env의 DATABASE_URL이 맞는지 확인하세요.\n  {error}"
        )
    except ValueError as error:
        raise SystemExit(str(error))

    if not users:
        print("사용자가 없습니다. 예시 프로필은 `python -m scripts.seed_dev_profiles`로 넣을 수 있습니다.")
        return

    headers = ("이름", "이메일", "환자", "user_id")
    rows = [(u.name, u.email, str(u.patient_count), str(u.id)) for u in users]
    sizes = [max(width(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    indent = " " * (sizes[0] + 2)

    print("  ".join(pad(h, s) for h, s in zip(headers, sizes)))
    print("  ".join("-" * s for s in sizes))
    for user, row in zip(users, rows):
        print("  ".join(pad(v, s) for v, s in zip(row, sizes)))
        # 담당 환자는 사용자 줄 아래에 한 줄씩 (등록 순, get_user()의 patient_ids와 같은 순서)
        for patient in uf.list_patients(user.id):
            stage = f" ({patient.dementia_stage})" if patient.dementia_stage else ""
            print(f"{indent}└ 환자 {patient.name or '(이름 없음)'}{stage}  patient_id = {patient.id}")
    print(f"\n{len(users)}명 (offset {args.offset})")


if __name__ == "__main__":
    main()
