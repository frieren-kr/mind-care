"""Convert the existing RAG guideline list without adding retrieval policy."""
from collections.abc import Callable

from pydantic import BaseModel

from ..schemas import AgentContext
from ..tools.contracts import EvidencePackage


class RagGuidelineRetriever:
    """Resolve RAG lazily; preserve ordering, duplicates, metadata and errors."""

    def __init__(self, *, search_fn: Callable[..., list[BaseModel]] | None = None):
        self.search_fn = search_fn

    def search_guidelines(self, *, query: str, top_k: int, context: AgentContext) -> EvidencePackage:
        search = self.search_fn
        if search is None:
            from app.ai.retrieval.search import search_guidelines as search

        # Identity remains server-side. RAG itself strips query before searching.
        raw_items = search(query=query, k=top_k)
        if not isinstance(raw_items, list) or any(not isinstance(item, BaseModel) for item in raw_items):
            raise TypeError("Guideline search must return a list of models")
        return EvidencePackage.model_validate({
            "query": query.strip(),
            "evidence": [item.model_dump(mode="json", warnings=False) for item in raw_items],
        })
