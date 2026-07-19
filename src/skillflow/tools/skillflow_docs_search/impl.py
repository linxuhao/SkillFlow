"""skillflow_docs_search — grep skillflow docs/schema, line-numbered snippets (native)."""
from skillflow.docs import search_docs


def skillflow_docs_search(query: str = "", **kwargs) -> dict:
    return search_docs(query)
