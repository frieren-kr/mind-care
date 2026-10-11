"""동기 확장 지점의 대기 시간 제한. 실행 중인 Python thread를 강제 종료하지 않는다."""
from contextvars import ContextVar, copy_context
from math import isfinite
from queue import Empty, Queue
from threading import BoundedSemaphore, Event, Thread, TIMEOUT_MAX
from time import monotonic
from typing import Callable, TypeVar

T = TypeVar("T")
request_deadline: ContextVar[float | None] = ContextVar("agent_request_deadline", default=None)
# 멈춘 Handler가 있어도 요청마다 무제한 thread를 만들지 않는다.
_WORKER_SLOTS = BoundedSemaphore(32)
_HANDLER_SLOTS = BoundedSemaphore(16)


class ExecutionTimeout(TimeoutError):
    pass


class RequestTimeout(ExecutionTimeout):
    pass


def positive_seconds(value: float, name: str) -> float:
    if type(value) not in (int, float) or not 0 < value <= TIMEOUT_MAX or not isfinite(value):
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def check_deadline() -> None:
    deadline = request_deadline.get()
    if deadline is not None and monotonic() >= deadline:
        raise RequestTimeout("Agent request deadline exceeded")


def bounded_call(function: Callable[[], T], timeout_seconds: float | None = None, *,
                 slots: BoundedSemaphore | None = None) -> T:
    """제한 내 결과만 수락한다. 늦은 결과는 폐기하며 자동 재실행하지 않는다."""
    check_deadline()
    now = monotonic()
    deadline = request_deadline.get()
    local_end = now + timeout_seconds if timeout_seconds is not None else deadline
    if local_end is None:
        raise ValueError("An execution timeout or request deadline is required")
    end = min(local_end, deadline) if deadline is not None else local_end
    request_bound = deadline is not None and deadline <= local_end

    def expired():
        if request_bound:
            raise RequestTimeout("Agent request deadline exceeded")
        raise ExecutionTimeout("Execution timeout exceeded")

    slots = _WORKER_SLOTS if slots is None else slots
    if not slots.acquire(timeout=max(0, end - monotonic())):
        expired()
    context = copy_context()
    results: Queue = Queue(maxsize=1)
    abandoned = Event()

    def worker():
        try:
            if abandoned.is_set() or monotonic() >= end:
                return
            try:
                value = context.run(function)
                result = (True, value)
            except Exception as exc:
                result = (False, exc)
            if not abandoned.is_set():
                results.put_nowait(result)
        finally:
            slots.release()

    try:
        Thread(target=worker, daemon=True, name="mindcare-agent-call").start()
    except Exception:
        slots.release()
        raise
    try:
        success, value = results.get(timeout=max(0, end - monotonic()))
        if monotonic() >= end:
            expired()
    except Empty:
        expired()
    finally:
        abandoned.set()
    if not success:
        raise value
    return value


def bounded_handler(function: Callable[[], T], timeout_seconds: float) -> T:
    # Executor worker와 Handler가 같은 slot을 서로 기다리는 상황을 방지한다.
    return bounded_call(function, timeout_seconds, slots=_HANDLER_SLOTS)
