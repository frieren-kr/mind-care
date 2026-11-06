"""최소 개발 범위의 프로세스별 로그인 제한. 다중 워커 운영에는 공유 제한 필요."""
from collections import deque
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request


class LoginLimiter:
    def __init__(self, limit: int = 10, window: int = 60, max_clients: int = 4096):
        self.limit, self.window, self.max_clients = limit, window, max_clients
        self._attempts: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, client: str) -> None:
        now = monotonic()
        with self._lock:
            for key in list(self._attempts):
                entries = self._attempts[key]
                while entries and entries[0] <= now - self.window:
                    entries.popleft()
                if not entries:
                    del self._attempts[key]
            entries = self._attempts.get(client)
            if (entries is not None and len(entries) >= self.limit) or (
                entries is None and len(self._attempts) >= self.max_clients
            ):
                raise HTTPException(429, "로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.",
                                    headers={"Retry-After": str(self.window)})
            self._attempts.setdefault(client, deque()).append(now)


login_limiter = LoginLimiter()


def check_login_limit(request: Request) -> None:
    login_limiter.check(request.client.host if request.client else "unknown")
