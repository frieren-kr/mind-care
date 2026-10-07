"""API 라우터 모음. 기능별로 파일을 나눈다 (papers.py, ...).

새 기능을 추가할 때:
  1) 이 폴더에 파일을 만들고 `router = APIRouter(prefix="/...", tags=["..."])`를 둔다.
  2) backend/main.py에서 `app.include_router(<파일>.router)`로 등록한다.
DB는 app/db/ 의 데이터 접근 함수로만 접근한다 (직접 SQL 금지).
"""
