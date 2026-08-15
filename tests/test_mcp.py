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
import clangd_query_engine as qe  # noqa: E402  (no mcp dependency, always available)

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

    def test_outline_and_diagnostics_require_a_path(self):
        for tool in ("get_file_outline", "get_diagnostics"):
            out = clangq_mcp._run_tool(tool, {"root": self.root})
            self.assertIn("path is required", out, "%s: %s" % (tool, out))

    def test_diagnostics_rejects_unknown_severity(self):
        out = clangq_mcp._run_tool(
            "get_diagnostics",
            {"root": self.root, "path": "a.cpp", "severity": "critical"})
        self.assertIn("severity must be one of", out)

    def test_outline_rejects_out_of_range_limit(self):
        for bad, expected in ((0, "at least 1"),
                              (clangq_mcp.MAX_OUTLINE_LIMIT + 1, "must not exceed")):
            out = clangq_mcp._run_tool(
                "get_file_outline",
                {"root": self.root, "path": "a.cpp", "limit": bad})
            self.assertIn(expected, out, "limit=%r: %s" % (bad, out))

    def test_outline_rejects_non_integer_limit(self):
        for bad in ("300", True):
            out = clangq_mcp._run_tool(
                "get_file_outline",
                {"root": self.root, "path": "a.cpp", "limit": bad})
            self.assertIn("must be an integer", out, "limit=%r: %s" % (bad, out))

    def test_new_tools_are_advertised_with_their_schemas(self):
        outline = clangq_mcp.TOOLS["get_file_outline"].as_mcp_tool()
        self.assertEqual(outline.inputSchema["required"], ["root", "path"])

        diagnostics = clangq_mcp.TOOLS["get_diagnostics"].as_mcp_tool()
        self.assertEqual(diagnostics.inputSchema["required"], ["root", "path"])
        self.assertEqual(
            sorted(diagnostics.inputSchema["properties"]["severity"]["enum"]),
            sorted(clangq_mcp.SEVERITY_FILTERS))

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
            def __init__(self, root, request_timeout=None):
                self.root = root
                self.request_timeout = request_timeout
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

    def test_client_gets_a_real_per_request_timeout(self):
        """MCP takes no --req-timeout flag, so ClangdClient's own 10s default
        silently capped every request until this was wired through -- see
        REQUEST_TIMEOUT_S."""
        client = clangq_mcp.get_client("/repoA")
        self.assertEqual(client.request_timeout, clangq_mcp.REQUEST_TIMEOUT_S)

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


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed: %s" % MCP_IMPORT_ERROR)
class TestClientIdleLifecycle(unittest.TestCase):
    """A warm clangd holds a full index in RAM (1-4GB on a large repo). An
    assistant that touches several repos in a session must not accumulate
    one clangd per repo forever: idle clients get reaped, the number of
    simultaneously warm roots is capped, and every client is reachable
    through exactly one normalized key regardless of path spelling."""

    def setUp(self):
        importlib.reload(clangq_mcp)
        self.created = []
        outer = self

        class FakeClangd:
            def __init__(self, root, request_timeout=None):
                self.root = root
                self.request_timeout = request_timeout
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
        clangq_mcp._last_used.clear()
        clangq_mcp._client_locks.clear()

    def test_path_spelling_variants_share_one_client(self):
        """'/repo' and '/repo/' (or a different case on Windows) are the
        same repo and must not each spawn their own clangd."""
        a = clangq_mcp.get_client(support.CORPUS_DIR)
        b = clangq_mcp.get_client(support.CORPUS_DIR + os.sep)
        self.assertIs(a, b)
        self.assertEqual(len(self.created), 1)

    def test_last_used_is_recorded_on_every_call(self):
        clangq_mcp.get_client("/repoA")
        key = clangq_mcp._normalize_root("/repoA")
        self.assertIn(key, clangq_mcp._last_used)

    def test_shutdown_all_clients_clears_last_used_too(self):
        """A stale entry left in _last_used after shutdown would make the
        idle reaper (or the LRU evictor) try to act on a client that no
        longer exists."""
        clangq_mcp.get_client("/repoA")
        clangq_mcp.shutdown_all_clients()
        self.assertEqual(clangq_mcp._clients, {})
        self.assertEqual(clangq_mcp._last_used, {})

    def test_shutdown_all_clients_shuts_down_every_client(self):
        clangq_mcp.get_client("/repoA")
        clangq_mcp.get_client("/repoB")
        clangq_mcp.shutdown_all_clients()
        self.assertTrue(all(c.shutdown_called for c in self.created))

    def test_shutdown_all_clients_is_idempotent(self):
        clangq_mcp.get_client("/repoA")
        clangq_mcp.shutdown_all_clients()
        clangq_mcp.shutdown_all_clients()  # must not raise on an empty cache

    def test_idle_client_is_reaped(self):
        clangq_mcp.get_client("/repoA")
        key = clangq_mcp._normalize_root("/repoA")
        far_future = clangq_mcp._last_used[key] + clangq_mcp.CLIENT_IDLE_TIMEOUT_S + 1
        reaped = clangq_mcp._reap_idle_clients(now=far_future)
        self.assertEqual(reaped, 1)
        self.assertNotIn(key, clangq_mcp._clients)
        self.assertTrue(self.created[0].shutdown_called)

    def test_fresh_client_is_not_reaped(self):
        clangq_mcp.get_client("/repoA")
        reaped = clangq_mcp._reap_idle_clients(now=time.monotonic())
        self.assertEqual(reaped, 0)
        self.assertFalse(self.created[0].shutdown_called)

    def test_reap_only_affects_clients_past_the_timeout(self):
        clangq_mcp.get_client("/repoA")
        key_a = clangq_mcp._normalize_root("/repoA")
        # Backdate A past the timeout; leave B fresh.
        clangq_mcp._last_used[key_a] -= clangq_mcp.CLIENT_IDLE_TIMEOUT_S + 1
        clangq_mcp.get_client("/repoB")

        reaped = clangq_mcp._reap_idle_clients(now=time.monotonic())
        self.assertEqual(reaped, 1)
        self.assertEqual(len(clangq_mcp._clients), 1)
        self.assertTrue(self.created[0].shutdown_called, "idle client A should be reaped")
        self.assertFalse(self.created[1].shutdown_called, "fresh client B should survive")

    def test_capacity_cap_evicts_the_least_recently_used(self):
        clangq_mcp.MAX_WARM_CLIENTS = 2
        clangq_mcp.get_client("/repoA")
        clangq_mcp.get_client("/repoB")
        # Touch B again so A becomes the least recently used.
        time.sleep(0.01)
        clangq_mcp.get_client("/repoB")
        time.sleep(0.01)
        clangq_mcp.get_client("/repoC")  # over capacity: should evict A, not B

        self.assertEqual(len(clangq_mcp._clients), 2)
        client_a = self.created[0]
        self.assertTrue(client_a.shutdown_called, "least-recently-used client was not evicted")
        key_b = clangq_mcp._normalize_root("/repoB")
        key_c = clangq_mcp._normalize_root("/repoC")
        self.assertIn(key_b, clangq_mcp._clients)
        self.assertIn(key_c, clangq_mcp._clients)

    def test_capacity_cap_does_not_evict_below_the_limit(self):
        clangq_mcp.MAX_WARM_CLIENTS = 8
        for i in range(clangq_mcp.MAX_WARM_CLIENTS):
            clangq_mcp.get_client("/repo%d" % i)
        self.assertEqual(len(clangq_mcp._clients), clangq_mcp.MAX_WARM_CLIENTS)
        self.assertTrue(all(not c.shutdown_called for c in self.created))

    def test_start_idle_reaper_starts_exactly_one_thread(self):
        """Called from main() -- must be safe to call more than once
        without accumulating background threads."""
        calls = []

        def fake_loop():
            calls.append(1)

        real_loop = clangq_mcp._idle_reaper_loop
        clangq_mcp._idle_reaper_loop = fake_loop
        try:
            clangq_mcp.start_idle_reaper()
            clangq_mcp.start_idle_reaper()
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.01)
        finally:
            clangq_mcp._idle_reaper_loop = real_loop

        self.assertEqual(len(calls), 1)

    def test_atexit_shutdown_is_registered(self):
        """D9's fix: shutdown_all_clients must run even if the server exits
        some way other than main()'s own `finally` below."""
        import inspect
        self.assertIn("atexit.register(shutdown_all_clients)", inspect.getsource(clangq_mcp))

    def test_main_shuts_down_clients_and_starts_the_reaper(self):
        import inspect
        source = inspect.getsource(clangq_mcp.main)
        self.assertIn("start_idle_reaper()", source)
        self.assertIn("shutdown_all_clients()", source)


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed: %s" % MCP_IMPORT_ERROR)
class TestCliMatchesMcpFeatureSet(unittest.TestCase):
    """clangd_query_engine.py (the CLI) and clangq_mcp.py (the MCP server)
    are two front ends on the same query engine. A query reachable from one
    but not the other defeats the point of shipping both, so this pins every
    MCP tool to a CLI equivalent and checks it's not just same-named but
    reaches the identical engine function."""

    # get_function_info/get_class_info/get_macro_info are reached through the
    # CLI's refs/callers/all subcommands plus --class/--macro (QUERY_KINDS),
    # not a same-named CLI_TOOLS entry.
    COVERED_BY_QUERY_KINDS = {"get_function_info", "get_class_info", "get_macro_info"}

    # mcp tool name -> (CLI subcommand name, engine *_as_string function name)
    MCP_TOOL_TO_CLI_TOOL = {
        "get_struct_info": ("struct", "run_struct_query_as_string"),
        "search_symbols": ("search", "run_search_query_as_string"),
        "get_file_outline": ("outline", "run_outline_query_as_string"),
        "get_diagnostics": ("diagnostics", "run_diagnostics_query_as_string"),
        "get_hover_info": ("hover", "run_hover_query_as_string"),
        "get_incoming_calls": ("incoming", "run_incoming_calls_query_as_string"),
    }

    def test_every_mcp_tool_has_a_cli_equivalent(self):
        missing = [t for t in clangq_mcp.TOOLS
                  if t not in self.COVERED_BY_QUERY_KINDS
                  and t not in self.MCP_TOOL_TO_CLI_TOOL]
        self.assertEqual(missing, [], "MCP tools with no known CLI subcommand: %s" % missing)

        for mcp_name, (cli_name, _) in self.MCP_TOOL_TO_CLI_TOOL.items():
            self.assertIn(mcp_name, clangq_mcp.TOOLS, "%s is not an MCP tool" % mcp_name)
            self.assertIn(cli_name, qe.CLI_TOOLS, "%s has no CLI subcommand" % cli_name)

    def test_no_cli_tool_is_left_out_of_the_mapping(self):
        """Catches the mirror mistake: a CLI subcommand added without a
        matching entry above, which would let it silently drift from MCP."""
        self.assertEqual(set(qe.CLI_TOOLS),
                         {cli for cli, _ in self.MCP_TOOL_TO_CLI_TOOL.values()})

    def test_mapped_tools_call_the_same_engine_function(self):
        """Same name is not the same feature -- both front ends must call
        the identical *_as_string function, or their answers could diverge."""
        import inspect
        for mcp_name, (cli_name, fn_name) in self.MCP_TOOL_TO_CLI_TOOL.items():
            with self.subTest(mcp_name):
                engine_fn = getattr(qe, fn_name)
                self.assertIs(qe.CLI_TOOLS[cli_name].as_string_fn, engine_fn,
                             "%s's CLI tool does not call %s" % (cli_name, fn_name))
                mcp_source = inspect.getsource(clangq_mcp.TOOLS[mcp_name].handler)
                self.assertIn(fn_name, mcp_source,
                             "%s's MCP handler does not appear to call %s" % (mcp_name, fn_name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
