"""Unit tests for web_search and web_fetch native stepflow tools.

Tests use mocking to avoid real network calls.
"""

import json
import pytest
from unittest.mock import patch, MagicMock


class TestWebSearch:
    def test_search_returns_results(self):
        from stepflow.tools.web_search.impl import web_search

        mock_data = {
            "results": [
                {"title": "Result 1", "url": "http://example.com/1",
                 "content": "First result content"},
                {"title": "Result 2", "url": "http://example.com/2",
                 "content": "Second result content"},
                {"title": "Result 3", "url": "http://example.com/3",
                 "content": "Third result content"},
            ],
            "number_of_results": 3,
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data

        with patch("httpx.get", return_value=mock_resp):
            result = web_search("test query", max_results=2)

        assert result["query"] == "test query"
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "Result 1"
        assert result["results"][1]["url"] == "http://example.com/2"

    def test_search_max_results_clamped(self):
        from stepflow.tools.web_search.impl import web_search

        mock_data = {"results": [], "number_of_results": 0}
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data

        with patch("httpx.get", return_value=mock_resp):
            result = web_search("q", max_results=100)  # clamped to 10
        # Should not raise — clamped

    def test_search_timeout_returns_error(self):
        from stepflow.tools.web_search.impl import web_search
        import httpx

        with patch("httpx.get", side_effect=httpx.TimeoutException("timed out")):
            result = web_search("test")
        assert "error" in result
        assert result["results"] == []

    def test_search_generic_error_returns_error(self):
        from stepflow.tools.web_search.impl import web_search

        with patch("httpx.get", side_effect=RuntimeError("boom")):
            result = web_search("test")
        assert "error" in result

    def test_search_schema_loads(self):
        from stepflow.tool_loader import ToolLoader
        from pathlib import Path

        tools_dir = Path(__file__).parent.parent / "src" / "stepflow" / "tools"
        loader = ToolLoader(tools_dir)
        schema = loader.load_schema("web_search")
        assert schema["name"] == "web_search"
        assert "query" in schema["parameters"]


class TestWebFetch:
    def test_fetch_html_extracts_text(self):
        from stepflow.tools.web_fetch.impl import web_fetch

        html = (
            "<html><head><title>Test Page</title></head>"
            "<body><h1>Hello</h1><p>World</p>"
            "<script>console.log('x')</script></body></html>"
        )
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.headers = {"content-type": "text/html"}

        with patch("httpx.get", return_value=mock_resp):
            result = web_fetch("http://example.com")

        assert result["title"] == "Test Page"
        assert "Hello" in result["content"]
        assert "World" in result["content"]
        assert "console.log" not in result["content"]  # script stripped

    def test_fetch_truncates_long_content(self):
        from stepflow.tools.web_fetch.impl import web_fetch

        mock_resp = MagicMock()
        mock_resp.text = "<html><body>" + "x" * 2000 + "</body></html>"
        mock_resp.headers = {"content-type": "text/html"}

        with patch("httpx.get", return_value=mock_resp):
            result = web_fetch("http://example.com", max_chars=100)

        assert len(result["content"]) <= 120  # 100 + "... [truncated]" + newlines
        assert result["truncated"] is True

    def test_fetch_blocks_private_ip(self):
        from stepflow.tools.web_fetch.impl import web_fetch

        result = web_fetch("http://127.0.0.1/secret")
        assert "error" in result
        assert "private" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_fetch_rejects_non_http_scheme(self):
        from stepflow.tools.web_fetch.impl import web_fetch

        result = web_fetch("ftp://example.com/file")
        assert "error" in result

    def test_fetch_invalid_url(self):
        from stepflow.tools.web_fetch.impl import web_fetch

        result = web_fetch("not-a-valid-url")
        assert "error" in result

    def test_fetch_timeout_returns_error(self):
        from stepflow.tools.web_fetch.impl import web_fetch
        import httpx

        with patch("httpx.get", side_effect=httpx.TimeoutException("timed out")):
            result = web_fetch("http://example.com")

        assert "error" in result

    def test_fetch_schema_loads(self):
        from stepflow.tool_loader import ToolLoader
        from pathlib import Path

        tools_dir = Path(__file__).parent.parent / "src" / "stepflow" / "tools"
        loader = ToolLoader(tools_dir)
        schema = loader.load_schema("web_fetch")
        assert schema["name"] == "web_fetch"
        assert "url" in schema["parameters"]


class TestWebToolsRegression:
    """Regression tests that both tools load via ToolLoader."""

    def test_both_tools_in_list(self):
        from stepflow.tool_loader import ToolLoader
        from pathlib import Path

        tools_dir = Path(__file__).parent.parent / "src" / "stepflow" / "tools"
        loader = ToolLoader(tools_dir)
        names = loader.list_tools()
        assert "web_search" in names
        assert "web_fetch" in names

    def test_both_tools_load_fn(self):
        from stepflow.tool_loader import ToolLoader
        from pathlib import Path

        tools_dir = Path(__file__).parent.parent / "src" / "stepflow" / "tools"
        loader = ToolLoader(tools_dir)

        search_fn = loader.load_fn("web_search")
        assert callable(search_fn)

        fetch_fn = loader.load_fn("web_fetch")
        assert callable(fetch_fn)

    def test_web_search_accepts_kwargs(self):
        """web_search must accept **kwargs (workspace_root injection)."""
        from stepflow.tools.web_search.impl import web_search

        mock_data = {"results": [], "number_of_results": 0}
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data

        with patch("httpx.get", return_value=mock_resp):
            result = web_search("q", workspace_root="/tmp")
        assert "results" in result

    def test_web_fetch_accepts_kwargs(self):
        """web_fetch must accept **kwargs (workspace_root injection)."""
        from stepflow.tools.web_fetch.impl import web_fetch

        mock_resp = MagicMock()
        mock_resp.text = "<html><body>test</body></html>"
        mock_resp.headers = {"content-type": "text/html"}

        with patch("httpx.get", return_value=mock_resp):
            result = web_fetch("http://example.com", workspace_root="/tmp")
        assert "content" in result
