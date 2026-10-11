"""RAG 경계의 인터페이스. 임베딩/검색/DB를 여기서 구현하지 않는다."""
from typing import Protocol

from ..schemas import AgentContext
from .contracts import EvidencePackage


class EvidenceRetriever(Protocol):
    def search_evidence(self, *, query: str, top_k: int, context: AgentContext) -> EvidencePackage:
        """RAG 담당 서비스가 제공할 계약. 현재 구현/운영 등록은 없다."""
        ...
