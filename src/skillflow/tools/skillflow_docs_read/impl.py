"""skillflow_docs_read — read a skillflow doc topic with line numbers (native)."""
from skillflow.docs import read_doc


def skillflow_docs_read(topic: str = "", start_line: int = 0,
                        end_line: int | None = None, **kwargs) -> dict:
    return read_doc(topic, start_line=start_line, end_line=end_line)
