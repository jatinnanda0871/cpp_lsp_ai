"""Tests for the MCP server layer (clangq_mcp.py).

Covers argument handling, warm-client caching, and the concurrency path that
actually broke in production: the host firing several tool calls at once, each
dispatched to a worker thread against one shared clangd.

Skipped when the `mcp` package is not installed (it is only needed for the
server, not the CLI).

    python3 -m unittest tests.test_mcp -v
"""
import importlib
import os
import sys
import threading
import time
import unittest

# So `python3 -m unittest tests.test_mcp` works from the repo root, where
# tests/ is not on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402  (needs the path set up first)

try:
    import clangq_mcp
    MCP_AVAILABLE = True
    MCP_IMPORT_ERROR = ""
except Exception as e:          # pragma: no cover - depends on environment
    MCP_AVAILABLE = False
    MCP_IMPORT_ERROR = repr(e)


class StubClient:
    """Stands in for a warm ClangdClient."""
    _db_found = True

    def __init__(self, alive=True):
        self._alive = alive
        self.shutdown_called = False

    def is_alive(self):
        return self._alive

    def shutdown(self):
        self.shutdown_called = True


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed: %s" % MCP_IMPORT_ERROR)
class TestArgumentHandling(unittest.TestCase):
    """Bad arguments must produce an explanatory string, never a raw
    exception -- the assistant on the other end can act on the former."""

    def setUp(self):
        self._real_get_client = clangq_mcp.get_client
        clangq_mcp.get_client = lambda root: StubClient()
        # root is validated as an existing directory before any tool runs,
        # so use a real one and keep these tests about the OTHER arguments
        self.root = support.CORPUS_DIR

    def tearDown(self):
        clangq_mcp.get_client = self._real_get_client

    def test_missing_root(self):
        self.assertIn("root is required", clangq_mcp._run_tool("get_function_info", {}))

    def test_unknown_tool(self):
        self.assertIn("Unknown tool", clangq_mcp._run_tool("bogus", {"root": self.root}))

    def test_missing_name_for_each_symbol_tool(self):
        for tool in ("get_function_info", "get_class_info", "get_macro_info",
                     "get_struct_info", "get_incoming_calls"):
            out = clangq_mcp._run_tool(tool, {"root": self.root})
            self.assertIn("name is required", out, "%s: %s" % (tool, out))

    def test_null_name_is_rejected(self):
        out = clangq_mcp._run_tool("get_class_info", {"root": self.root, "name": None})
        self.assertIn("name is required", out)

    def test_hover_requires_path(self):
        out = clangq_mcp._run_tool("get_hover_info", {"root": self.root, "line": 1, "col": 2})
        self.assertIn("path is required", out)

    def test_hover_rejects_null_coordinates(self):
        """The reported failure: line=None reached 'line + 1'."""
        out = clangq_mcp._run_tool(
            "get_hover_info", {"root": self.root, "path": "a.cpp", "line": None, "col": 3})
        self.assertIn("line is required", out)

    def test_hover_rejects_string_coordinates(self):
        out = clangq_mcp._run_tool(
            "get_hover_info", {"root": self.root, "path": "a.cpp", "line": "12", "col": 3})
        self.assertIn("must be an integer", out)

    def test_search_requires_a_query(self):
        for args in ({}, {"query": None}, {"query": "  "}):
            args = dict(args, root=self.root)
            out = clangq_mcp._run_tool("search_symbols", args)
            self.assertIn("query is required", out, "%r: %s" % (args, out))

    def test_search_rejects_unknown_kind(self):
        """Must not fall through to an unfiltered search -- results would
        ignore the constraint while appearing to honour it."""
        out = clangq_mcp._run_tool(
            "search_symbols", {"root": self.root, "query": "x", "kind": "klass"})
        self.assertIn("kind must be one of", out)

    def test_search_rejects_blank_kind(self):
        """Blank is a caller mistake on an OPTIONAL parameter, not an
        omission: swapping it for the default would hide the bug."""
        out = clangq_mcp._run_tool(
            "search_symbols", {"root": self.root, "query": "x", "kind": ""})
        self.assertIn("kind must be one of", out)

    def test_search_rejects_out_of_range_limit(self):
        for bad, expected in ((0, "at least 1"), (-3, "at least 1"),
                              (clangq_mcp.MAX_SEARCH_LIMIT + 1, "must not exceed")):
            out = clangq_mcp._run_tool(
                "search_symbols", {"root": self.root, "query": "x", "limit": bad})
            self.assertIn(expected, out, "limit=%r: %s" % (bad, out))

    def test_search_rejects_non_integer_limit(self):
        for bad in ("30", True, 1.5):
            out = clangq_mcp._run_tool(
                "search_symbols", {"root": self.root, "query": "x", "limit": bad})
            self.assertIn("must be an integer", out, "limit=%r: %s" % (bad, out))

    def test_search_is_advertised_with_its_schema(self):
        """The tool must reach the host's tool list, and its enum must match
        the filters the engine actually accepts."""
        spec = clangq_mcp.TOOLS["search_symbols"].as_mcp_tool()
        self.assertEqual(spec.inputSchema["required"], ["root", "query"])
        self.assertEqual(sorted(spec.inputSchema["properties"]["kind"]["enum"]),
                         sorted(clangq_mcp.SEARCH_KIND_FILTERS))


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed: %s" % MCP_IMPORT_ERROR)
class TestMissingCompileDatabase(unittest.TestCase):
    """Without compile_commands.json clangd starts fine but indexes nothing,
    so every query comes back empty forever. That must be stated, not left
    for the caller to misread as 'no matches'."""

    def setUp(self):
        self._real_get_client = clangq_mcp.get_client

        class NoDb(StubClient):
            _db_found = False

        clangq_mcp.get_client = lambda root: NoDb()
        self.root = support.CORPUS_DIR

    def tearDown(self):
        clangq_mcp.get_client = self._real_get_client

    def test_warning_is_surfaced(self):
        out = clangq_mcp._run_tool("get_function_info", {"root": self.root, "name": "x"})
        self.assertIn("no compile_commands.json", out)


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed: %s" % MCP_IMPORT_ERROR)
class TestWarmClientCache(unittest.TestCase):
    """One clangd per repo root, reused across calls, replaced when it dies."""

    def setUp(self):
        importlib.reload(clangq_mcp)
        self.created = []
        outer = self

        class FakeClangd:
            def __init__(self, root):
                self.root = root
                self._alive = True
                self.shutdown_called = False
                outer.created.append(self)

            def start(self):
                return self

            def prime_index(self):
                pass

            def is_alive(self):
                return self._alive

            def shutdown(self):
                self.shutdown_called = True

        self.FakeClangd = FakeClangd
        clangq_mcp.ClangdClient = FakeClangd
        clangq_mcp._clients.clear()
        clangq_mcp._client_locks.clear()

    def test_warm_client_is_reused(self):
        first = clangq_mcp.get_client("/repoA")
        self.assertIs(first, clangq_mcp.get_client("/repoA"))
        self.assertEqual(len(self.created), 1)

    def test_dead_client_is_replaced(self):
        """A crashed clangd used to stay cached, so every later call for that
        root failed forever until the server was restarted."""
        first = clangq_mcp.get_client("/repoA")
        first._alive = False
        second = clangq_mcp.get_client("/repoA")
        self.assertIsNot(second, first)
        self.assertTrue(first.shutdown_called, "dead client was not shut down")
        self.assertEqual(len(self.created), 2)

    def test_distinct_roots_get_distinct_clients(self):
        self.assertIsNot(clangq_mcp.get_client("/repoA"), clangq_mcp.get_client("/repoB"))

    def test_locks_are_per_root(self):
        a = clangq_mcp._lock_for("/repoA")
        self.assertIsNot(a, clangq_mcp._lock_for("/repoB"))
        self.assertIs(a, clangq_mcp._lock_for("/repoA"))

    def test_concurrent_cold_start_creates_one_client(self):
        class SlowClangd(self.FakeClangd):
            def prime_index(self):
                time.sleep(0.2)

        clangq_mcp.ClangdClient = SlowClangd
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            clangq_mcp.get_client("/repoC"))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.created), 1, "cold start raced and spawned extra clangd")
        self.assertEqual(len({id(r) for r in results}), 1)

    def test_slow_cold_start_does_not_block_a_different_root(self):
        """The cache lock is per-root: indexing repo A must not stall repo B."""
        gate = threading.Event()

        class BlockingClangd(self.FakeClangd):
            def prime_index(self):
                if self.root == "/slow":
                    gate.wait(5)

        clangq_mcp.ClangdClient = BlockingClangd
        slow = threading.Thread(target=lambda: clangq_mcp.get_client("/slow"))
        slow.start()
        time.sleep(0.1)

        done = threading.Event()
        threading.Thread(target=lambda: (clangq_mcp.get_client("/fast"), done.set())).start()
        got_through = done.wait(2)

        gate.set()
        slow.join(5)
        self.assertTrue(got_through, "a cold start on one root blocked another root")


if __name__ == "__main__":
    unittest.main(verbosity=2)
