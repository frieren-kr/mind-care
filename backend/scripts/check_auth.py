"""실행 중인 서버의 로그인·본인 접근·로그아웃 점검. 비밀번호·토큰을 출력하지 않는다.

python -m scripts.check_auth --base-url http://127.0.0.1:8000
추천까지 확인하려면 --recommendations (실제 검색/LLM이 실행될 수 있음).
"""
import argparse
from getpass import getpass
from uuid import uuid4

import httpx


def check(response: httpx.Response, expected: int, label: str) -> httpx.Response:
    if response.status_code != expected:
        raise RuntimeError(f"{label}: 기대 HTTP {expected}, 실제 HTTP {response.status_code}")
    print(f"[통과] {label}: HTTP {expected}")
    return response


def run_checks(client, email: str, password: str, *, recommendations: bool = False) -> None:
    response = check(client.post("/auth/login", json={"email": email, "password": password}), 200, "로그인")
    body = response.json()
    headers = {"Authorization": "Bearer " + body["access_token"]}
    user_id = body["user"]["id"]
    logged_out = False
    try:
        current = check(client.get("/auth/me", headers=headers), 200, "내 정보").json()
        if current["id"] != user_id:
            raise RuntimeError("로그인 사용자와 내 정보의 ID가 다릅니다.")
        check(client.get(f"/feed/{user_id}", headers=headers), 200, "본인 프로필")
        check(client.get(f"/users/{user_id}/feed-settings", headers=headers), 200, "본인 피드 설정")
        check(client.get(f"/feed/{user_id}"), 401, "토큰 없는 접근 차단")
        other_id = uuid4()
        check(client.get(f"/feed/{other_id}", headers=headers), 403, "다른 사용자 경로 차단")
        check(client.patch(f"/users/{other_id}/feed-settings", json={"feed_refresh_days": 5}, headers=headers),
              403, "다른 사용자 설정 변경 차단")
        if recommendations:
            recommendation = check(client.get(f"/recommendations/{user_id}?k=1", headers=headers),
                                   200, "본인 추천 피드")
            print(f"추천 응답 방식: {recommendation.json()['mode']}")
        check(client.post("/auth/logout", headers=headers), 200, "로그아웃")
        logged_out = True
        check(client.get("/auth/me", headers=headers), 401, "로그아웃한 토큰 차단")
        check(client.post("/auth/logout", headers=headers), 200, "로그아웃 반복 호출")
    finally:
        if not logged_out:
            # 중간 점검 실패 시에도 이 실행에서 발급한 세션은 폐기한다.
            try:
                check(client.post("/auth/logout", headers=headers), 200, "점검 세션 정리")
            except (httpx.HTTPError, RuntimeError):
                print("점검 세션 정리 실패: 토큰 만료 시까지 세션이 남을 수 있습니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--recommendations", action="store_true")
    args = parser.parse_args()
    email = input("테스트 계정 이메일: ").strip()
    password = getpass("비밀번호: ")
    try:
        with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=120, follow_redirects=False) as client:
            run_checks(client, email, password, recommendations=args.recommendations)
    except httpx.HTTPError:
        raise SystemExit("서버 연결 또는 응답 대기에 실패했습니다. 서버 주소와 실행 상태를 확인하세요.") from None
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    print("인증 점검 완료. 토큰을 폐기했으며 기존 프로필·설정은 변경하지 않았습니다.")


if __name__ == "__main__":
    main()
