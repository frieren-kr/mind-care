"""DB 연결 관리. pgvector 확장을 사용하는 PostgreSQL에 접속한다.

주의: 팀원(AI/백엔드)은 이 모듈을 직접 import 하지 말고,
functions.py의 데이터 접근 함수만 사용한다.
"""
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector

from app.config import settings


@contextmanager
def get_connection():
    """psycopg 커넥션을 열고 pgvector 타입을 등록한 뒤 반환한다."""
    conn = psycopg.connect(settings.database_url)
    try:
        register_vector(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
