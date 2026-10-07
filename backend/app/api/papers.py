"""논문 API (박주현 담당).

GET /papers            논문 목록 (최신 발행일 순, limit/offset)
GET /papers/{paper_id} 논문 상세 + 요약
"""
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.db import functions
from app.schemas import PaperDetailResponse, PaperListResponse

router = APIRouter(prefix="/papers", tags=["papers"])


@router.get("", response_model=PaperListResponse, summary="논문 목록")
def list_papers(
    limit: int = Query(20, ge=1, le=100, description="한 번에 받을 개수 (최대 100)"),
    offset: int = Query(0, ge=0, description="건너뛸 개수"),
) -> PaperListResponse:
    """최신 발행일 순 논문 목록. 초록은 빠져 있다 (상세에서 받는다)."""
    return functions.list_papers(limit=limit, offset=offset)


@router.get(
    "/{paper_id}",
    response_model=PaperDetailResponse,
    summary="논문 상세",
    responses={
        404: {
            "description": "없는 paper_id",
            "content": {"application/json": {"example": {"detail": "논문을 찾을 수 없습니다."}}},
        }
    },
)
def get_paper(paper_id: UUID) -> PaperDetailResponse:
    """논문 상세 + 요약 3칸. 요약이 아직 없으면 요약 쪽 필드는 null."""
    paper = functions.get_paper_detail(paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="논문을 찾을 수 없습니다.")
    return paper
