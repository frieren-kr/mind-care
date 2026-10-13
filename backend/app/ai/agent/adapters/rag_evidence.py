"""Mechanical conversion from the existing RAG search to Agent evidence."""
from collections.abc import Callable

from pydantic import BaseModel

from ..schemas import AgentContext
from ..tools.contracts import EvidencePackage


class RagEvidenceRetriever:
    """Use RAG's filters, ordering and metadata without adding search policy.

    With no injected search_fn, resolve the real RAG search at execution time.
    Construction does not load an embedding model or connect to the database.
    Search/conversion exceptions propagate to the existing ToolExecutor.
    """

    def __init__(self, *, search_fn: Callable[..., BaseModel] | None = None):
        self.search_fn = search_fn

    def search_evidence(self, *, query: str, top_k: int, context: AgentContext) -> EvidencePackage:
        search = self.search_fn
        if search is None:
            from app.ai.retrieval.search import search

        # Context stays server-side: the existing RAG API takes no identity.
        raw = search(query=query, k=top_k)
        return EvidencePackage.model_validate(raw.model_dump(mode="json", warnings=False))
