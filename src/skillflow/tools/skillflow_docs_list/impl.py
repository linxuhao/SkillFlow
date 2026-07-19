"""skillflow_docs_list — enumerate skillflow doc topics (native)."""
from skillflow.docs import list_topics


def skillflow_docs_list(**kwargs) -> dict:
    return list_topics()
