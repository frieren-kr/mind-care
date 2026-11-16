"""Mechanical conversion from the existing RAG search to Agent evidence."""
import logging
from collections.abc import Callable

from httpx import TimeoutException
from openai import APITimeoutError
from pydantic import BaseModel, ValidationError

from ..error_diagnostics import summarize_validation_error
from ..executor import ToolOutputValidationError
from ..schemas import AgentContext
from ..tools.contracts import EvidencePackage


logger = logging.getLogger(__name__)


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
        try:
            return EvidencePackage.model_validate(raw.model_dump(mode="json", warnings=False))
        except (TimeoutError, TimeoutException, APITimeoutError):
            # RequestTimeout also inherits TimeoutError. Preserve the exact
            # exception so the Executor/Runner keep their existing stop policy.
            raise
        except Exception as exc:
            logger.info("agent_error request_id=%r stage=rag_result_conversion input_origin=rag_output "
                        "error_code=invalid_output exception_type=%s diagnostic=%s",
                        context.request_id, type(exc).__name__,
                        summarize_validation_error(exc, EvidencePackage) if isinstance(exc, ValidationError) else {})
            raise ToolOutputValidationError("Tool output does not satisfy its contract") from exc
