"""기존 업무 회귀 테스트 전용. 인증 자체는 test_auth_*에서 실제 의존성으로 검증한다."""
from app.auth.dependencies import require_user_access


def allow_business_test_requests(test, app):
    previous = app.dependency_overrides.copy()

    def restore():
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    test.addCleanup(restore)
    app.dependency_overrides[require_user_access] = lambda: None
