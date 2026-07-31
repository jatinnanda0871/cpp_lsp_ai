"""Unit tests: no clangd, no C++ toolchain, no network -- always runnable.

These cover the parsing/formatting/lifecycle logic with hand-built LSP
payloads, including the malformed and explicitly-null shapes a real language
server can emit. Every test here corresponds to a bug that was actually hit
(a crash, an empty answer, or a wrong answer), so they are regression guards
rather than speculative coverage.

    python3 -m unittest tests.test_unit -v
"""
import io
import json
import os
import tempfile
import threading
import types
import unittest

import support

import clangd_query_engine as qe
from clangd_client import ClangdClient


def blank_client():
    """A ClangdClient with its fields initialised but no subprocess."""
    c = ClangdClient.__new__(ClangdClient)
    c.log = False
    c.req_timeout = 2
    c._id = 0
    c._id_lock = threading.Lock()
    c._pending = {}
    c._pending_lock = threading.Lock()
    c._opened = set()
    c._index_warm = False
    c.root = os.getcwd()
    c.proc = None
    return c


def framed(obj):
    body = json.dumps(obj).encode()
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def fake_proc(stream=b"", poll=0, stdin=None):
    return types.SimpleNamespace(stdout=io.BytesIO(stream),
                                 stdin=stdin, poll=lambda: poll)


class FakeQueryClient:
    """Mimics the ClangdClient surface the query engine consumes."""
    def __init__(self, symbols=None, docsym=None, decl=None, root="/repo"):
        self._symbols = symbols or []
        self._docsym = docsym
        self._decl = decl
        self.root = root

    def resolve_symbol(self, name, deadline_s=10):
        return self._symbols

    def document_symbol_async(self, path):
        return ("doc", path)

    def declaration_async(self, path, line, col):
        return ("decl", path)

    def definition_async(self, path, line, col):
        return ("def", path)

    def references_async(self, *a):
        return ("refs", None)

    def prepare_calls_async(self, *a):
        return ("calls", None)

    def incoming_async(self, *a):
        return ("inc", None)

    def wait_result(self, handle, timeout=None):
        return {"doc": self._docsym, "decl": self._decl}.get(handle[0])

    _find_node_by_range_start = ClangdClient._find_node_by_range_start
    end_line_from_document_symbol = ClangdClient.end_line_from_document_symbol


SYMBOL = [{
    "name": "Foo", "kind": 12,
    "location": {"uri": "file:///repo/a.cpp",
                 "range": {"start": {"line": 10, "character": 2}}},
}]


# --------------------------------------------------------------------------
class TestMalformedLspPayloads(unittest.TestCase):
    """A language server may omit fields or send explicit nulls. None of it
    may reach arithmetic or string concatenation."""

    def test_pick_symbol_kind_on_empty_list(self):
        # was IndexError -> surfaced as "Error running query: list index out of range"
        self.assertIsNone(qe.pick_symbol_kind([], "Foo", 5))

    def test_loc_str_with_null_range(self):
        self.assertTrue(qe.loc_str({"uri": "file:///t/a.cpp", "range": None}))

    def test_loc_str_with_null_start(self):
        self.assertTrue(qe.loc_str({"uri": "file:///t/a.cpp", "range": {"start": None}}))

    def test_loc_str_with_null_line(self):
        # was TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'
        out = qe.loc_str({"uri": "file:///t/a.cpp", "range": {"start": {"line": None}}})
        self.assertTrue(out.endswith(":1"), out)

    def test_find_node_tolerates_null_start_and_non_dict(self):
        c = ClangdClient.__new__(ClangdClient)
        self.assertIsNone(
            ClangdClient._find_node_by_range_start(c, [{"range": {"start": None}}, "junk"], 5))

    def test_end_line_tolerates_null_end(self):
        c = ClangdClient.__new__(ClangdClient)
        nodes = [{"range": {"start": {"line": 3}, "end": None}}]
        self.assertIsNone(ClangdClient.end_line_from_document_symbol(c, nodes, 3))

    def test_find_class_methods_tolerates_null_ranges(self):
        c = ClangdClient.__new__(ClangdClient)
        tree = [{"range": {"start": {"line": 0}, "end": {"line": 9}},
                 "children": [{"kind": 6, "name": "m1",
                               "selectionRange": {"start": None},
                               "range": {"end": None}}]}]
        methods = ClangdClient._find_class_methods(c, tree, 0)
        self.assertEqual(methods[0]["start_line"], 0)

    def test_resolve_position_never_returns_none_end_line(self):
        """documentSymbol not matching the symbol's line used to leave the end
        line as None, which then blew up in '(def path:start-end)'."""
        c = FakeQueryClient(symbols=SYMBOL, docsym=[])
        for row in qe.resolve_position(c, "Foo", 1, False):
            self.assertIsNotNone(row[3])

    def test_resolve_position_with_null_declaration_range(self):
        c = FakeQueryClient(symbols=SYMBOL, docsym=[],
                            decl=[{"uri": "file:///repo/a.h", "range": None}])
        rows = qe.resolve_position(c, "Foo", 1, False)
        self.assertEqual(rows[0][5], 0)

    def test_class_query_with_null_method_name(self):
        """A child symbol with name=None used to raise TypeError on 'Cls::' + None."""
        class C(FakeQueryClient):
            def get_class_end_and_methods(self, path, start_line):
                return 20, [{"name": None, "start_line": 3, "start_col": 1, "end_line": 5}]
            def wait_result(self, handle, timeout=None):
                return None if handle[0] == "def" else []

        cls = [{"name": "Cls", "kind": 5,
                "location": {"uri": "file:///repo/a.h", "range": {"start": {"line": 0}}}}]
        out = io.StringIO()
        qe.run_class_query(C(symbols=cls), "all", "Cls", 1, out=out)
        self.assertIn("unnamed", out.getvalue())


class TestEmptyResultsAreReported(unittest.TestCase):
    """A query that finds nothing must SAY so. Returning an empty string makes
    the MCP tool look broken and tells the assistant nothing."""

    def test_run_query_reports_no_match(self):
        out = io.StringIO()
        qe.run_query(FakeQueryClient(symbols=[]), "refs", "Nope", 1, out=out)
        self.assertIn("no symbol matching", out.getvalue())

    def test_run_class_query_reports_no_match(self):
        out = io.StringIO()
        qe.run_class_query(FakeQueryClient(symbols=[]), "all", "Nope", 1, out=out)
        self.assertIn("no class found", out.getvalue())


class TestDisplayPaths(unittest.TestCase):
    """Reported paths must depend on the repo root, never the process cwd:
    the MCP host chooses the server's working directory, so cwd-relative
    output names the same file differently from run to run."""

    URI = "file:///repo/src/a.cpp" if os.name != "nt" else "file:///C:/repo/src/a.cpp"
    ROOT = "/repo" if os.name != "nt" else r"C:\repo"

    def test_uri_to_path_is_absolute(self):
        p = qe.uri_to_path(self.URI)
        self.assertTrue(os.path.isabs(p), p)

    def test_display_path_is_root_relative(self):
        p = qe.display_path(qe.uri_to_path(self.URI), self.ROOT)
        self.assertEqual(p, os.path.join("src", "a.cpp"))

    def test_display_path_is_cwd_independent(self):
        """The actual regression: same input, different cwd, same output."""
        seen = set()
        original = os.getcwd()
        try:
            for d in (support.TESTS_DIR, support.REPO_ROOT, tempfile.gettempdir()):
                os.chdir(d)
                seen.add(qe.display_path(qe.uri_to_path(self.URI), self.ROOT))
        finally:
            os.chdir(original)
        self.assertEqual(len(seen), 1, "path varied with cwd: %s" % seen)

    def test_file_outside_root_stays_absolute(self):
        other = "file:///elsewhere/x.cpp" if os.name != "nt" else "file:///C:/elsewhere/x.cpp"
        p = qe.display_path(qe.uri_to_path(other), self.ROOT)
        self.assertTrue(os.path.isabs(p), p)

    def test_no_base_leaves_path_absolute(self):
        p = qe.display_path(qe.uri_to_path(self.URI), None)
        self.assertTrue(os.path.isabs(p), p)


class TestTransportFraming(unittest.TestCase):
    """A malformed frame must fail loudly and wake every waiter -- silently
    killing the reader thread leaves in-flight requests hanging to timeout."""

    def test_missing_content_length(self):
        c = blank_client()
        c.proc = fake_proc(b"X-Foo: 1\r\n\r\n{}")
        with self.assertRaises(RuntimeError) as ctx:
            c._read_one()
        self.assertIn("Content-Length", str(ctx.exception))

    def test_non_integer_content_length(self):
        c = blank_client()
        c.proc = fake_proc(b"Content-Length: abc\r\n\r\n{}")
        with self.assertRaises(RuntimeError) as ctx:
            c._read_one()
        self.assertIn("Content-Length", str(ctx.exception))

    def test_reader_failure_wakes_pending_waiters(self):
        c = blank_client()
        c.proc = fake_proc(b"Content-Length: zzz\r\n\r\n{}", poll=3)
        ev, holder = threading.Event(), {}
        c._pending[1] = (ev, holder)
        c._reader()
        self.assertTrue(ev.is_set())
        self.assertIn("error", holder)

    def test_non_dict_message_is_skipped(self):
        c = blank_client()
        c.proc = fake_proc(framed([1, 2, 3]) + framed({"id": 7, "result": {"ok": 1}}))
        ev, holder = threading.Event(), {}
        c._pending[7] = (ev, holder)
        c._reader()
        self.assertEqual(holder.get("result"), {"ok": 1})

    def test_notifications_with_null_params(self):
        c = blank_client()
        c.log = True
        c.proc = fake_proc(framed({"method": "$/progress", "params": None})
                           + framed({"method": "window/logMessage", "params": None})
                           + framed({"id": 9, "result": "done"}))
        ev, holder = threading.Event(), {}
        c._pending[9] = (ev, holder)
        err, real = io.StringIO(), None
        import sys
        real, sys.stderr = sys.stderr, err
        try:
            c._reader()
        finally:
            sys.stderr = real
        self.assertEqual(holder.get("result"), "done")

    def test_send_failure_does_not_leak_a_pending_entry(self):
        class BrokenStdin:
            def write(self, b):
                raise BrokenPipeError("broken pipe")
            def flush(self):
                pass

        c = blank_client()
        c.proc = fake_proc(stdin=BrokenStdin(), poll=1)
        with self.assertRaises(RuntimeError):
            c._request_async("textDocument/hover", {})
        self.assertEqual(len(c._pending), 0, "pending entry leaked after failed send")


class TestFileHandling(unittest.TestCase):
    def test_did_open_missing_file_names_the_path(self):
        c = blank_client()
        missing = os.path.join(tempfile.gettempdir(), "definitely_absent_9f2a.cpp")
        with self.assertRaises(RuntimeError) as ctx:
            c.did_open(missing)
        self.assertIn("cannot open source file", str(ctx.exception))

    def test_prime_index_with_non_list_database(self):
        c = blank_client()
        c._db_found = True
        c._db_path = self._write_db({"not": "a list"})
        self.assertIsNone(c.prime_index())

    def test_prime_index_with_junk_entries(self):
        c = blank_client()
        c._db_found = True
        c._db_path = self._write_db(["junk", {"no_file_key": 1}, {"file": "/nope/gone.cpp"}])
        self.assertIsNone(c.prime_index())

    def _write_db(self, payload):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "compile_commands.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path


class TestMacroDefinitionScan(unittest.TestCase):
    """clangd only reports a macro once the defining file is OPEN, so the
    client locates the #define itself. Without this, get_macro_info burned
    the whole timeout and then answered 'no macro found' for a macro that
    plainly exists."""

    def setUp(self):
        self.client = blank_client()
        self.client.root = support.CORPUS_DIR

    def test_finds_an_existing_macro(self):
        found = self.client.find_macro_definition_file("CORPUS_MAX_ITEMS")
        self.assertIsNotNone(found, "did not locate #define CORPUS_MAX_ITEMS")
        self.assertTrue(found.endswith("util.h"), found)

    def test_finds_a_function_like_macro(self):
        self.assertIsNotNone(self.client.find_macro_definition_file("CORPUS_CLAMP"))

    def test_returns_none_for_unknown_macro(self):
        self.assertIsNone(self.client.find_macro_definition_file("NO_SUCH_MACRO_ZZ"))

    def test_does_not_match_a_prefix(self):
        """CORPUS_MAX must not match '#define CORPUS_MAX_ITEMS'."""
        self.assertIsNone(self.client.find_macro_definition_file("CORPUS_MAX"))

    def test_does_not_match_a_usage_site(self):
        """Only the #define counts, not the places the macro is used."""
        found = self.client.find_macro_definition_file("CORPUS_MAX_ITEMS")
        self.assertTrue(found.endswith("util.h"),
                        "matched a usage site instead of the definition: %s" % found)


class TestClientLifecycle(unittest.TestCase):
    def test_is_alive(self):
        c = ClangdClient.__new__(ClangdClient)
        c.proc = None
        self.assertFalse(c.is_alive())
        c.proc = types.SimpleNamespace(poll=lambda: 1)
        self.assertFalse(c.is_alive(), "exited process reported alive")
        c.proc = types.SimpleNamespace(poll=lambda: None)
        self.assertTrue(c.is_alive())

    def test_shutdown_on_a_client_that_never_started(self):
        c = blank_client()
        c.shutdown()  # must not raise

    def test_shutdown_reaps_the_process_and_closes_pipes(self):
        """terminate() without wait() leaves a zombie, and unclosed pipes leak
        file descriptors -- both accumulate in a long-lived MCP server that
        replaces a clangd whenever one dies."""
        closed = []

        class Stream:
            def __init__(self, tag):
                self.tag = tag
            def close(self):
                closed.append(self.tag)

        class Proc:
            def __init__(self):
                self.waited = False
                self.stdin = Stream("stdin")
                self.stdout = Stream("stdout")
                self.stderr = Stream("stderr")
            def wait(self, timeout=None):
                self.waited = True
                return 0
            def poll(self):
                return None
            def terminate(self):
                pass
            def kill(self):
                pass

        c = blank_client()
        c.proc = Proc()
        c.shutdown()
        self.assertTrue(c.proc.waited, "process was never reaped with wait()")
        self.assertEqual(sorted(closed), ["stderr", "stdin", "stdout"])

    def test_shutdown_wakes_pending_requests(self):
        c = blank_client()
        c.proc = types.SimpleNamespace(
            stdin=None, stdout=None, stderr=None,
            wait=lambda timeout=None: 0, poll=lambda: None,
            terminate=lambda: None, kill=lambda: None)
        ev, holder = threading.Event(), {}
        # a high id, so shutdown's own request does not reuse it
        c._pending[9999] = (ev, holder)
        c.shutdown()
        self.assertTrue(ev.is_set(), "shutdown left a request waiting for a reply")


class TestWarmIndexFastFail(unittest.TestCase):
    """A cold index deserves the full deadline; a warm one does not. An MCP
    call passes 120s, so without this a mistyped name blocks a worker thread
    for two minutes."""

    def _client(self, warm):
        c = blank_client()
        c._index_warm = warm
        c.workspace_symbol = lambda q: []
        return c

    def test_cold_client_uses_the_full_deadline(self):
        import time
        c = self._client(False)
        t0 = time.time()
        self.assertEqual(c.resolve_symbol("nope", deadline_s=3, interval_s=1), [])
        self.assertGreaterEqual(time.time() - t0, 2.0)

    def test_warm_client_gives_up_early(self):
        import time
        c = self._client(True)
        t0 = time.time()
        self.assertEqual(c.resolve_symbol("nope", deadline_s=120, interval_s=1), [])
        elapsed = time.time() - t0
        self.assertLess(elapsed, 20, "warm miss took %.1fs, expected a fast give-up" % elapsed)

    def test_workspace_symbol_marks_the_index_warm(self):
        c = blank_client()
        c._request = lambda method, params, timeout=None: [{"name": "x"}]
        self.assertFalse(c._index_warm)
        c.workspace_symbol("x")
        self.assertTrue(c._index_warm)

    def test_a_miss_does_not_mark_the_index_warm(self):
        c = blank_client()
        c._request = lambda method, params, timeout=None: []
        c.workspace_symbol("x")
        self.assertFalse(c._index_warm)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCliAndDaemonDispatch(unittest.TestCase):
    """The CLI and the daemon must agree on what --class and --macro mean.

    They did not: the daemon protocol carried only `is_class`, so `--macro`
    was silently ignored on the default (daemon-backed) path and the query
    ran as a plain function lookup.
    """

    def _args(self, **kwargs):
        defaults = dict(is_class=False, is_macro=False, mode="all", name="X",
                        wait=10, include_decl=False, verbose=False,
                        root=".", ccdir=None, clangd="clangd", req_timeout=10,
                        no_daemon=False)
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def test_symbol_kind_from_flags(self):
        self.assertEqual(qe.symbol_kind(self._args()), "function")
        self.assertEqual(qe.symbol_kind(self._args(is_class=True)), "class")
        self.assertEqual(qe.symbol_kind(self._args(is_macro=True)), "macro")

    def test_every_kind_has_a_query_function(self):
        for kind in ("function", "class", "macro"):
            self.assertIn(kind, qe.QUERY_KINDS)
            self.assertTrue(callable(qe.QUERY_KINDS[kind]))

    def test_daemon_request_carries_the_symbol_kind(self):
        """The regression: --macro has to survive the trip to the daemon."""
        sent = {}

        class FakeConn:
            def close(self):
                pass

        original_send, original_recv = qe._send_json, qe._recv_line
        original_connect = qe._try_connect
        qe._send_json = lambda conn, obj: sent.update(obj)
        qe._recv_line = lambda conn: '{"out": ""}'
        qe._try_connect = lambda sock_path: FakeConn()
        try:
            qe.daemon_query(self._args(is_macro=True, name="LOG_DEBUG"))
        finally:
            qe._send_json, qe._recv_line = original_send, original_recv
            qe._try_connect = original_connect

        self.assertEqual(sent.get("kind"), "macro",
                         "daemon request dropped the symbol kind: %r" % sent)

    def test_daemon_dispatches_each_known_command(self):
        for cmd in ("ping", "shutdown", "query"):
            self.assertIn(cmd, qe.DAEMON_COMMANDS)

    def test_daemon_shutdown_stops_serving(self):
        _, keep_serving = qe.DAEMON_COMMANDS["shutdown"](None, {}, self._args())
        self.assertFalse(keep_serving)

    def test_daemon_ping_keeps_serving(self):
        response, keep_serving = qe.DAEMON_COMMANDS["ping"](None, {}, self._args())
        self.assertTrue(keep_serving)
        self.assertTrue(response.get("ok"))

    def test_daemon_rejects_an_unknown_query_kind(self):
        response, keep_serving = qe.DAEMON_COMMANDS["query"](
            None, {"kind": "bogus", "name": "X"}, self._args())
        self.assertIn("unknown query kind", response.get("error", ""))
        self.assertTrue(keep_serving, "a bad request must not stop the daemon")

    def test_session_modes_are_all_handled(self):
        for mode in qe.SESSION_MODES:
            self.assertIn(mode, qe.SESSION_HANDLERS)

    def test_parser_accepts_every_declared_mode(self):
        parser = qe.build_parser()
        for mode in qe.QUERY_MODES:
            self.assertEqual(parser.parse_args([mode, "sym"]).mode, mode)
        for mode in qe.SESSION_MODES:
            self.assertEqual(parser.parse_args([mode]).mode, mode)


class TestHoverContentShapes(unittest.TestCase):
    """hover's `contents` is MarkupContent, a MarkedString, or an array of
    either -- all three shapes have to render."""

    def test_plain_string(self):
        self.assertEqual(qe._hover_text("int foo()"), "int foo()")

    def test_markup_content_dict(self):
        self.assertIn("int foo()", qe._hover_text({"kind": "markdown", "value": "int foo()"}))

    def test_marked_string_with_language(self):
        out = qe._hover_text({"language": "cpp", "value": "int foo()"})
        self.assertIn("cpp", out)
        self.assertIn("int foo()", out)

    def test_array_of_marked_strings(self):
        out = qe._hover_text([{"language": "cpp", "value": "int foo()"}, {"value": "docs"}])
        self.assertIn("int foo()", out)
        self.assertIn("docs", out)

    def test_array_of_plain_strings(self):
        self.assertIn("abc", qe._hover_text(["abc"]))

    def test_empty_dict_does_not_crash(self):
        self.assertIsInstance(qe._hover_text({}), str)

    def test_none_does_not_crash(self):
        self.assertIsInstance(qe._hover_text(None), str)


class TestMacroExpansionDetection(unittest.TestCase):
    """The '#define' line itself is not an expansion site; a reference list
    containing only it means clangd found no uses."""

    def ref(self, path, line):
        return {"uri": "file:///" + path.lstrip("/"),
                "range": {"start": {"line": line}}}

    def test_definition_line_is_not_an_expansion(self):
        decl = qe.uri_to_path("file:///r/util.h")
        refs = [self.ref("r/util.h", 10)]
        self.assertEqual(qe._expansion_sites(refs, decl, 10), [])

    def test_other_lines_count_as_expansions(self):
        decl = qe.uri_to_path("file:///r/util.h")
        refs = [self.ref("r/util.h", 10), self.ref("r/util.cpp", 4)]
        self.assertEqual(len(qe._expansion_sites(refs, decl, 10)), 1)


class TestInputPathResolution(unittest.TestCase):
    """Every query REPORTS paths relative to the repo root, so a caller
    naturally feeds one back in. get_hover_info passed it straight to open(),
    so the obvious round trip -- look up a function, hover at the location it
    just reported -- failed unless the process happened to be running in the
    repo root."""

    def setUp(self):
        self.client = FakeQueryClient(root=support.CORPUS_DIR)

    def test_relative_path_is_resolved_against_the_root(self):
        resolved = qe.resolve_input_path(self.client, os.path.join("src", "big.cpp"))
        self.assertTrue(os.path.isabs(resolved), resolved)
        self.assertTrue(os.path.exists(resolved), resolved)

    def test_absolute_path_is_left_alone(self):
        absolute = support.corpus_source("src/big.cpp")
        self.assertEqual(qe.resolve_input_path(self.client, absolute), absolute)

    def test_unknown_relative_path_is_left_alone(self):
        """Nothing to resolve against means the caller gets their own path
        back, and the real 'cannot open' error naming it."""
        self.assertEqual(qe.resolve_input_path(self.client, "nope/missing.cpp"),
                         "nope/missing.cpp")

    def test_empty_path_is_left_alone(self):
        self.assertEqual(qe.resolve_input_path(self.client, ""), "")

    def test_client_without_a_root(self):
        self.assertEqual(qe.resolve_input_path(FakeQueryClient(root=None), "a.cpp"), "a.cpp")


class TestFuzzyResultsAreLabelledAndBounded(unittest.TestCase):
    """A '::' query fuzzy-matched 18 symbols and printed all of them, every
    block titled '# ::' -- the query string rather than the symbol found."""

    def symbols(self, count):
        return [{"name": "Sym%02d" % i, "kind": 12,
                 "location": {"uri": "file:///repo/a%02d.cpp" % i,
                              "range": {"start": {"line": i, "character": 0}}}}
                for i in range(count)]

    def test_fuzzy_candidates_are_capped(self):
        client = FakeQueryClient(symbols=self.symbols(18), docsym=[])
        rows = qe.resolve_position(client, "::", 1, False, notes=[])
        self.assertLessEqual(len(rows), qe.MAX_FUZZY_CANDIDATES,
                             "fuzzy fallback expanded %d candidates" % len(rows))

    def test_exact_matches_are_not_capped(self):
        """The cap must only apply to guesses -- a genuine overload set of
        more than MAX_FUZZY_CANDIDATES must still come back whole."""
        syms = [{"name": "over", "kind": 12,
                 "location": {"uri": "file:///repo/a%d.cpp" % i,
                              "range": {"start": {"line": i, "character": 0}}}}
                for i in range(qe.MAX_FUZZY_CANDIDATES + 3)]
        client = FakeQueryClient(symbols=syms, docsym=[])
        rows = qe.resolve_position(client, "over", 1, False, notes=[])
        self.assertEqual(len(rows), qe.MAX_FUZZY_CANDIDATES + 3)

    def test_resolve_position_reports_the_found_name(self):
        client = FakeQueryClient(symbols=self.symbols(3), docsym=[])
        rows = qe.resolve_position(client, "::", 1, False, notes=[])
        self.assertTrue(all(row[6].startswith("Sym") for row in rows),
                        "found_name not carried out: %r" % [r[6] for r in rows])

    def test_note_says_how_many_were_suppressed(self):
        note = qe.inexact_match_note(self.symbols(5), "::", total=18)
        self.assertIn("5 of 18", note)

    def test_output_is_titled_with_the_symbol_found(self):
        """The regression: 18 blocks all titled '# ::'."""
        client = FakeQueryClient(symbols=self.symbols(3), docsym=[])
        out = io.StringIO()
        qe.run_query(client, "refs", "::", 1, out=out)
        text = out.getvalue()
        self.assertIn("Sym00", text)
        self.assertNotIn("# ::  (def", text)

    def test_exact_query_keeps_the_requested_label(self):
        """A qualified lookup must still read as 'Backend::process', not the
        bare 'process' clangd reports."""
        syms = [{"name": "process", "kind": 12, "containerName": "Backend",
                 "location": {"uri": "file:///repo/a.cpp",
                              "range": {"start": {"line": 1, "character": 0}}}}]
        client = FakeQueryClient(symbols=syms, docsym=[])
        out = io.StringIO()
        qe.run_query(client, "refs", "Backend::process", 1, out=out)
        self.assertIn("Backend::process", out.getvalue())
