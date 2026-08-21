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
import queue
import sys
import tempfile
import threading
import time
import types
import unittest

# So `python3 -m unittest tests.test_unit` works from the repo root, where
# tests/ is not on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402  (needs the path set up first)

import clangd_query_engine as qe
from clangd_client import ClangdClient


def blank_client():
    """A ClangdClient with its fields initialised but no subprocess."""
    c = ClangdClient.__new__(ClangdClient)
    c.log = False
    c.req_timeout = 2
    c._id = 0
    c._id_lock = threading.Lock()
    c._send_lock = threading.Lock()
    c._pending = {}
    c._pending_lock = threading.Lock()
    c._opened = {}
    c._open_lock = threading.Lock()
    c._diagnostics = {}
    c._diag_events = {}
    c._diag_lock = threading.Lock()
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


class TestMissingDeclarationOutput(unittest.TestCase):
    """A function with no separate declaration (no header, or clangd found
    nothing) used to print '(decl :1)' -- indistinguishable from a real
    location at line 1 of an unnamed file. It must say plainly that no
    declaration was found instead."""

    def test_no_declaration_is_stated_not_faked_as_a_location(self):
        # FakeQueryClient defaults decl=None, so declaration_async resolves
        # to nothing -- the exact shape textDocument/declaration returns for
        # a function with no separate header declaration.
        out = io.StringIO()
        qe.run_query(FakeQueryClient(symbols=SYMBOL, docsym=[]), "refs", "Foo", 1, out=out)
        text = out.getvalue()
        self.assertIn("(decl: not found)", text)
        self.assertNotIn("(decl :1)", text)
        self.assertNotIn("(decl :", text)


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


class TestSendConcurrency(unittest.TestCase):
    """O2: _send is called from many worker threads (the MCP server's
    thread pool; one thread per daemon connection since P1). Without
    _send_lock, two concurrent frames could interleave their bytes on the
    wire -- this proves the lock actually prevents that, via a deterministic
    handshake rather than hoping a race shows up under GIL scheduling."""

    def test_concurrent_sends_never_interleave_on_the_wire(self):
        """Thread A's write is paused mid-frame by a controlled handshake,
        and thread B's write gets a real window to run while A is paused.
        With _send_lock, B cannot even start writing until A's whole
        _send() call (lock held for its entire duration) has returned, so
        the frames can never interleave; without it, B's write would land
        inside A's."""
        first_write_started = threading.Event()
        resume_first_write = threading.Event()

        class HandshakeStdin:
            def __init__(self):
                self.chunks = []
                self._first_call_done = False

            def write(self, data):
                if not self._first_call_done:
                    self._first_call_done = True
                    mid = len(data) // 2
                    self.chunks.append(data[:mid])
                    first_write_started.set()
                    resume_first_write.wait(5)
                    self.chunks.append(data[mid:])
                else:
                    self.chunks.append(data)

            def flush(self):
                pass

        c = blank_client()
        stdin = HandshakeStdin()
        c.proc = types.SimpleNamespace(stdin=stdin)

        def send_a():
            c._notify("m", {"who": "A"})

        def send_b():
            first_write_started.wait(5)
            c._notify("m", {"who": "B"})

        ta = threading.Thread(target=send_a)
        tb = threading.Thread(target=send_b)
        ta.start()
        tb.start()

        # Give B a real window to attempt its write while A is paused
        # mid-frame, then let A finish.
        first_write_started.wait(5)
        time.sleep(0.1)
        resume_first_write.set()
        ta.join(5)
        tb.join(5)

        # Reassemble the raw byte stream and re-parse every frame. This only
        # succeeds if each write() landed as one whole, uninterrupted frame --
        # an interleaved write corrupts framing (a garbled Content-Length, or
        # a body that fails to parse as JSON).
        raw = b"".join(stdin.chunks)
        seen = []
        while raw:
            header_end = raw.index(b"\r\n\r\n")
            length = int(raw[:header_end].split(b"Content-Length:")[1].strip())
            body = raw[header_end + 4: header_end + 4 + length]
            seen.append(json.loads(body.decode("utf-8")))
            raw = raw[header_end + 4 + length:]

        self.assertEqual(sorted(m["params"]["who"] for m in seen), ["A", "B"])


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


class TestIncludeGraphScan(unittest.TestCase):
    """list_includes/find_includers are a pure text scan (see
    find_macro_definition_file above for the same tradeoff) -- no clangd
    needed, so these run against the real fixture corpus directly."""

    def setUp(self):
        self.client = blank_client()
        self.client.root = support.CORPUS_DIR

    def test_list_includes_of_a_known_file(self):
        shapes_h = support.corpus_source("include/shapes.h")
        includes = self.client.list_includes(shapes_h)
        self.assertIn(("prelude.h", False), includes,
                      "missing prelude.h in %r" % (includes,))

    def test_list_includes_distinguishes_system_from_quoted(self):
        path = os.path.join(support.TESTS_DIR, "_tmp_includes_test.h")
        with open(path, "w", encoding="utf-8") as f:
            f.write('#include <vector>\n#include "local.h"\n// #include "commented.h"\n')
        try:
            includes = self.client.list_includes(path)
        finally:
            os.remove(path)
        self.assertEqual(includes, [("vector", True), ("local.h", False)])

    def test_find_includers_of_a_known_file(self):
        shapes_h = support.corpus_source("include/shapes.h")
        includers = self.client.find_includers(shapes_h)
        found = [os.path.basename(p) for p, _line in includers]
        self.assertIn("shapes.cpp", found, "shapes.cpp does not include shapes.h in %r" % (found,))

    def test_find_includers_excludes_the_file_itself(self):
        prelude_h = support.corpus_source("include/prelude.h")
        includers = self.client.find_includers(prelude_h)
        for path, _line in includers:
            self.assertNotEqual(os.path.normpath(path), os.path.normpath(prelude_h))

    def test_list_includes_on_unreadable_file_raises(self):
        with self.assertRaises(RuntimeError):
            self.client.list_includes(os.path.join(support.TESTS_DIR, "no_such_file_zz.h"))


class TestRenameEditGrouping(unittest.TestCase):
    """_rename_edits_by_file must handle both WorkspaceEdit shapes clangd can
    send back -- only one is ever populated per reply."""

    def test_changes_shape(self):
        edit = {"changes": {"file:///a/b.cpp": [{"range": {"start": {"line": 0, "character": 0}}}]}}
        by_file = qe._rename_edits_by_file(edit)
        self.assertEqual(len(by_file), 1)
        [(path, edits)] = by_file.items()
        self.assertTrue(path.endswith("b.cpp"), path)
        self.assertEqual(len(edits), 1)

    def test_document_changes_shape(self):
        edit = {"documentChanges": [
            {"textDocument": {"uri": "file:///a/b.cpp", "version": 1},
             "edits": [{"range": {"start": {"line": 1, "character": 2}}}]},
        ]}
        by_file = qe._rename_edits_by_file(edit)
        self.assertEqual(len(by_file), 1)
        [(path, edits)] = by_file.items()
        self.assertTrue(path.endswith("b.cpp"), path)
        self.assertEqual(len(edits), 1)

    def test_empty_edit_produces_no_files(self):
        self.assertEqual(qe._rename_edits_by_file({}), {})

    def test_identifier_validation(self):
        for good in ("x", "_x", "camelCase", "Snake_Case9"):
            self.assertIsNotNone(qe._IDENTIFIER_RE.match(good), good)
        for bad in ("9x", "has space", "has-dash", "", "a.b"):
            self.assertIsNone(qe._IDENTIFIER_RE.match(bad), bad)


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

    def test_macro_fallback_that_still_misses_does_not_take_the_full_deadline(self):
        """The defining file is found and opened, but clangd still won't
        show the macro (e.g. it's inside #if 0, or outside
        compile_commands.json). That is not a cold-index problem, so
        retrying every interval_s until the caller's full deadline (120s
        from MCP) cannot fix it -- it must give up on a short leash instead."""
        import time
        c = self._client(False)
        c.find_macro_definition_file = lambda name: __file__  # any real, openable path
        c.did_open = lambda path: None
        t0 = time.time()
        self.assertEqual(c.resolve_macro("NOPE_MACRO", deadline_s=120, interval_s=1), [])
        elapsed = time.time() - t0
        self.assertLess(elapsed, 20,
                        "stalled macro fallback took %.1fs, expected a bounded give-up" % elapsed)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCliAndDaemonDispatch(unittest.TestCase):
    """The CLI and the daemon must agree on what --class and --macro mean.

    They did not: the daemon protocol carried only `is_class`, so `--macro`
    was silently ignored on the default (daemon-backed) path and the query
    ran as a plain function lookup.
    """

    def _args(self, **kwargs):
        defaults = dict(is_class=False, is_macro=False, command="all", name="X",
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
        for cmd in ("ping", "shutdown", "query", "tool"):
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
            self.assertEqual(parser.parse_args([mode, "sym"]).command, mode)
        for mode in qe.SESSION_MODES:
            self.assertEqual(parser.parse_args([mode]).command, mode)


class TestCliToolsRegistry(unittest.TestCase):
    """struct/search/outline/diagnostics/hover/incoming: the CLI subcommands
    that mirror clangq_mcp.py's tools beyond plain refs/callers/all. One
    table (CLI_TOOLS) drives the subparser, the in-process call, and the
    daemon call, so this is where a new tool could silently go missing from
    one of the three."""

    EXPECTED_TOOLS = ("struct", "search", "outline", "diagnostics", "hover", "incoming")

    def test_every_expected_tool_is_registered(self):
        for name in self.EXPECTED_TOOLS:
            self.assertIn(name, qe.CLI_TOOLS)
            tool = qe.CLI_TOOLS[name]
            self.assertTrue(callable(tool.configure))
            self.assertTrue(callable(tool.args_to_kwargs))
            self.assertTrue(callable(tool.as_string_fn))

    def test_parser_accepts_every_cli_tool(self):
        """Each subcommand parses with its minimal required arguments and
        selects the right command."""
        parser = qe.build_parser()
        cases = {
            "struct": ["struct", "Point"],
            "search": ["search", "Backend"],
            "outline": ["outline", "a.cpp"],
            "diagnostics": ["diagnostics", "a.cpp"],
            "hover": ["hover", "a.cpp", "10", "2"],
            "incoming": ["incoming", "doThing"],
        }
        for name, argv in cases.items():
            args = parser.parse_args(argv)
            self.assertEqual(args.command, name)

    def test_hover_coordinates_are_parsed_as_integers(self):
        parser = qe.build_parser()
        args = parser.parse_args(["hover", "a.cpp", "10", "2"])
        self.assertEqual((args.line, args.col), (10, 2))

    def test_args_to_kwargs_matches_as_string_fn_signature(self):
        """A kwarg name that doesn't match the target function's parameter
        name would raise TypeError only when actually run -- catch it here
        instead, for every registered tool at once."""
        import inspect
        common = types.SimpleNamespace(
            name="X", new_name="Y", query="X", path="a.cpp", line=1, col=2,
            kind=None, limit=10, severity="all", wait=5, verbose=False)
        for name, tool in qe.CLI_TOOLS.items():
            with self.subTest(name):
                kwargs = tool.args_to_kwargs(common)
                sig = inspect.signature(tool.as_string_fn)
                try:
                    sig.bind(client=object(), **kwargs)
                except TypeError as e:
                    self.fail("%s: args_to_kwargs produced %r, which does not "
                             "bind to %s: %s" % (name, kwargs, tool.as_string_fn, e))

    def test_daemon_tool_dispatches_to_the_right_tool(self):
        calls = []
        real = qe.CLI_TOOLS["struct"].as_string_fn
        qe.CLI_TOOLS["struct"].as_string_fn = lambda client, **kw: calls.append(kw) or "STRUCT OUT"
        try:
            response, keep_serving = qe.DAEMON_COMMANDS["tool"](
                "fake-client", {"tool": "struct", "kwargs": {"name": "Point", "wait": 1}},
                types.SimpleNamespace(verbose=False))
        finally:
            qe.CLI_TOOLS["struct"].as_string_fn = real

        self.assertTrue(keep_serving)
        self.assertEqual(response.get("out"), "STRUCT OUT")
        self.assertEqual(calls, [{"name": "Point", "wait": 1}])

    def test_daemon_rejects_an_unknown_tool(self):
        response, keep_serving = qe.DAEMON_COMMANDS["tool"](
            None, {"tool": "bogus"}, types.SimpleNamespace(verbose=False))
        self.assertIn("unknown tool", response.get("error", ""))
        self.assertTrue(keep_serving, "a bad request must not stop the daemon")

    def test_daemon_tool_reports_a_query_failure_without_raising(self):
        real = qe.CLI_TOOLS["hover"].as_string_fn

        def boom(client, **kw):
            raise RuntimeError("clangd exploded")
        qe.CLI_TOOLS["hover"].as_string_fn = boom
        try:
            response, keep_serving = qe.DAEMON_COMMANDS["tool"](
                "fake-client", {"tool": "hover", "kwargs": {}},
                types.SimpleNamespace(verbose=False))
        finally:
            qe.CLI_TOOLS["hover"].as_string_fn = real

        self.assertTrue(keep_serving)
        self.assertIn("clangd exploded", response.get("error", ""))

    def test_daemon_tool_sender_round_trips_kwargs(self):
        """The client-side counterpart to test_daemon_request_carries_the_
        symbol_kind: what daemon_tool() sends must be exactly what the
        server-side handler above expects to receive."""
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
            args = types.SimpleNamespace(
                root=".", no_daemon=False, wait=7, verbose=False, name="Point")
            qe.daemon_tool(args, qe.CLI_TOOLS["struct"])
        finally:
            qe._send_json, qe._recv_line = original_send, original_recv
            qe._try_connect = original_connect

        self.assertEqual(sent.get("cmd"), "tool")
        self.assertEqual(sent.get("tool"), "struct")
        self.assertEqual(sent.get("kwargs"), {"name": "Point", "wait": 7, "verbose": False})


class FakeConn:
    """Stands in for an accepted socket connection: the daemon transport
    only ever calls .recv()/.sendall()/.close() on one (see _recv_line /
    _send_json), so a real socket is not needed to test the request/
    response and threading logic around it."""

    def __init__(self, request_obj):
        payload = b"" if request_obj is None else (json.dumps(request_obj) + "\n").encode("utf-8")
        self._buf = payload
        self.sent = []
        self.closed = False

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True

    def response(self):
        return json.loads(b"".join(self.sent).decode("utf-8").strip())


class FakeServerSocket:
    """accept() blocks on a queue instead of a real socket. push(None)
    simulates the wake-up connection _signal_stop makes to unblock a real
    accept() -- it raises OSError, the same as accept() on a closed
    socket, which is what _accept_loop's own break condition expects."""

    def __init__(self):
        self._queue = queue.Queue()

    def push(self, conn):
        self._queue.put(conn)

    def accept(self):
        conn = self._queue.get()
        if conn is None:
            raise OSError("fake socket closed")
        return conn, None


class TestConcurrentDaemonServing(unittest.TestCase):
    """P1: the daemon used to serve one request at a time -- a slow
    get_class_info blocked every other CLI invocation against the same
    daemon for its whole duration, even though ClangdClient is explicitly
    built to pipeline concurrent requests. Each connection now runs on its
    own thread; these tests prove that end to end without a real socket
    (AF_UNIX is unavailable on this platform's Python build)."""

    def _args(self):
        return types.SimpleNamespace(verbose=False)

    def test_signal_stop_sets_event_and_wakes_the_accept_loop(self):
        """accept() blocks; without the self-connect, a shutdown request
        with no further connections would leave the loop hanging forever
        instead of noticing stop_event."""
        connected_to = []

        class FakeWakeSocket:
            def connect(self, path):
                connected_to.append(path)
            def close(self):
                pass

        had_af_unix = hasattr(qe.socket, "AF_UNIX")
        if not had_af_unix:
            qe.socket.AF_UNIX = 1  # opaque stand-in; _signal_stop only passes it through
        real_ctor = qe.socket.socket
        qe.socket.socket = lambda family, kind: FakeWakeSocket()
        stop_event = threading.Event()
        try:
            qe._signal_stop(stop_event, "/fake/sock/path")
        finally:
            qe.socket.socket = real_ctor
            if not had_af_unix:
                del qe.socket.AF_UNIX

        self.assertTrue(stop_event.is_set())
        self.assertEqual(connected_to, ["/fake/sock/path"])

    def test_requests_are_served_concurrently_not_serially(self):
        """The whole point of P1: a slow request must not delay the
        response to a second, concurrently-open connection."""
        gate = threading.Event()

        def _cmd_slow(client, req, args):
            gate.wait(5)
            return {"ok": True}, True

        qe.DAEMON_COMMANDS["__test_slow__"] = _cmd_slow
        try:
            srv = FakeServerSocket()
            slow_conn = FakeConn({"cmd": "__test_slow__"})
            fast_conn = FakeConn({"cmd": "ping"})
            srv.push(slow_conn)
            srv.push(fast_conn)

            thread = threading.Thread(
                target=qe._accept_loop,
                args=(srv, None, self._args(), "/fake/sock/path"), daemon=True)
            thread.start()
            try:
                for _ in range(300):
                    if fast_conn.sent:
                        break
                    time.sleep(0.01)
                self.assertTrue(fast_conn.sent,
                                "fast request never answered -- was it blocked behind the slow one?")
                self.assertTrue(fast_conn.response().get("ok"))
                self.assertFalse(slow_conn.sent, "slow request answered before its gate opened")
            finally:
                gate.set()
                srv.push(None)  # unblock accept() so the loop thread can exit
                thread.join(5)

            self.assertTrue(slow_conn.sent, "slow request never got its turn")
        finally:
            qe.DAEMON_COMMANDS.pop("__test_slow__", None)

    def test_shutdown_command_stops_the_accept_loop(self):
        srv = FakeServerSocket()
        shutdown_conn = FakeConn({"cmd": "shutdown"})
        srv.push(shutdown_conn)

        def fake_signal_stop(stop_event, sock_path):
            stop_event.set()
            srv.push(None)  # what a real self-connect would trigger

        real_signal_stop = qe._signal_stop
        qe._signal_stop = fake_signal_stop
        try:
            thread = threading.Thread(
                target=qe._accept_loop,
                args=(srv, None, self._args(), "/fake/sock/path"), daemon=True)
            thread.start()
            thread.join(5)
        finally:
            qe._signal_stop = real_signal_stop

        self.assertFalse(thread.is_alive(), "accept loop did not stop on shutdown")
        self.assertTrue(shutdown_conn.sent)
        self.assertTrue(shutdown_conn.response().get("ok"))

    def test_recv_daemon_response_turns_no_answer_into_a_clean_error(self):
        """A connection can land in the accept backlog at the same moment
        the daemon decides to stop (see _accept_loop's stop_event check --
        whether the loop notices before or after accepting one more
        connection is an inherent race no server-side effort fully closes).
        Whatever the daemon does or doesn't send back, the CLIENT must
        never crash on it: _recv_daemon_response is the actual backstop,
        not the server's best-effort courtesy message."""
        class EmptyConn:
            def recv(self, n):
                return b""  # what a closed connection with nothing sent looks like

        resp = qe._recv_daemon_response(EmptyConn())
        self.assertIn("error", resp)
        self.assertNotIn("Traceback", repr(resp))

        class GarbageConn:
            def recv(self, n):
                return b"not json at all\n"

        resp = qe._recv_daemon_response(GarbageConn())
        self.assertIn("error", resp)

    def test_a_bad_request_does_not_kill_the_accept_loop(self):
        """A malformed request must not stop the daemon -- it outlives its
        clients, so anything unhandled here would strand the warm index."""
        srv = FakeServerSocket()
        bad_conn = FakeConn(None)
        bad_conn._buf = b"not json\n"
        good_conn = FakeConn({"cmd": "ping"})
        srv.push(bad_conn)
        srv.push(good_conn)

        thread = threading.Thread(
            target=qe._accept_loop,
            args=(srv, None, self._args(), "/fake/sock/path"), daemon=True)
        thread.start()
        try:
            for _ in range(300):
                if good_conn.sent:
                    break
                time.sleep(0.01)
        finally:
            srv.push(None)
            thread.join(5)

        self.assertIn("malformed", bad_conn.response().get("error", ""))
        self.assertTrue(good_conn.response().get("ok"))


class FakeMacroClient:
    """Tracks call order to prove run_macro_query fires every macro's
    references_async before awaiting any of them (P3): N matches should
    cost ~1 round trip, not N serial ones. Every other multi-item path in
    the module (fetch_refs_and_callers, _class_method_items) already does
    this; run_macro_query used to be the one that didn't."""

    def __init__(self, macros):
        self._macros = macros
        self.root = "/repo"
        self.calls = []

    def resolve_macro(self, name, deadline_s=10):
        return self._macros

    def references_async(self, path, line, col, include_decl):
        self.calls.append(("fire", path))
        return ("refs", path)

    def wait_result(self, handle, timeout=None):
        self.calls.append(("await", handle[1]))
        return []


def _macro_sym(name, path, line):
    return {"name": name, "kind": 15,
           "location": {"uri": "file://" + path, "range": {"start": {"line": line, "character": 0}}}}


class TestMacroQueryConcurrency(unittest.TestCase):
    def test_fires_all_references_before_awaiting_any(self):
        macros = [_macro_sym("M1", "/repo/a.h", 1),
                 _macro_sym("M2", "/repo/b.h", 2),
                 _macro_sym("M3", "/repo/c.h", 3)]
        client = FakeMacroClient(macros)
        out = io.StringIO()
        qe.run_macro_query(client, "all", "M", 1, out=out)

        fires = [i for i, (kind, _) in enumerate(client.calls) if kind == "fire"]
        awaits = [i for i, (kind, _) in enumerate(client.calls) if kind == "await"]
        self.assertEqual(len(fires), 3)
        self.assertEqual(len(awaits), 3)
        self.assertLess(max(fires), min(awaits),
                        "a references_async fired after an earlier one was already "
                        "awaited -- the two-phase fetch collapsed back into serial "
                        "round trips:\n%r" % client.calls)

    def test_output_still_covers_every_macro_in_order(self):
        """The concurrency refactor must not change what gets printed --
        only when the network calls happen."""
        macros = [_macro_sym("M1", "/repo/a.h", 1), _macro_sym("M2", "/repo/b.h", 2)]
        client = FakeMacroClient(macros)
        out = io.StringIO()
        qe.run_macro_query(client, "all", "M", 1, out=out)
        text = out.getvalue()
        self.assertLess(text.index("M1"), text.index("M2"))

    def test_a_reference_failure_on_one_macro_does_not_affect_another(self):
        class FlakyClient(FakeMacroClient):
            def wait_result(self, handle, timeout=None):
                # "a.h", not the raw fixture path -- uri_to_path normalizes
                # separators per-platform (e.g. backslashes on Windows).
                if "a.h" in handle[1]:
                    raise RuntimeError("boom")
                return super().wait_result(handle, timeout=timeout)

        macros = [_macro_sym("M1", "/repo/a.h", 1), _macro_sym("M2", "/repo/b.h", 2)]
        client = FlakyClient(macros)
        out = io.StringIO()
        qe.run_macro_query(client, "all", "M", 1, out=out)
        text = out.getvalue()
        self.assertIn("query failed", text)
        self.assertIn("0 references of M2", text)


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


class TestSymbolSearch(unittest.TestCase):
    """search_symbols is the discovery entry point, so it inverts two rules
    the other queries follow: a fuzzy hit is the answer rather than a warning,
    and the output is meant to be fed straight back into the other tools."""

    def sym(self, name, kind=12, container="", line=0, uri="file:///repo/a.cpp"):
        return {"name": name, "kind": kind, "containerName": container,
                "location": {"uri": uri,
                             "range": {"start": {"line": line, "character": 0}}}}

    def search(self, symbols, query, **kwargs):
        client = FakeQueryClient(symbols=symbols, root="/repo")
        out = io.StringIO()
        qe.run_search_query(client, query, out=out, **kwargs)
        return out.getvalue()

    # ---- names must be usable by the other tools ----
    def test_names_come_back_qualified(self):
        """The whole point of the tool: 'process' alone would resolve to some
        other class's overload when fed to get_function_info."""
        text = self.search([self.sym("process", 6, "chain::Backend")], "process")
        self.assertIn("chain::Backend::process", text)

    def test_bare_name_when_there_is_no_container(self):
        text = self.search([self.sym("parseInt")], "parseInt")
        self.assertIn("parseInt", text)
        self.assertNotIn("::parseInt", text)

    def test_container_already_in_the_name_is_not_doubled(self):
        text = self.search(
            [self.sym("Backend::process", 6, "Backend")], "process")
        self.assertIn("Backend::process", text)
        self.assertNotIn("Backend::Backend::process", text)

    # ---- exact vs fuzzy ----
    def test_exact_and_fuzzy_are_sectioned(self):
        text = self.search(
            [self.sym("process"), self.sym("processedCount")], "process")
        self.assertIn("# exact", text)
        self.assertIn("# fuzzy", text)

    def test_exact_matches_win_the_limit_budget(self):
        """Truncation that dropped the symbol the caller literally named
        would be worse than no answer at all."""
        symbols = ([self.sym("hit", 12, "N%d" % i) for i in range(3)] +
                   [self.sym("hitAdjacent%d" % i) for i in range(10)])
        text = self.search(symbols, "hit", limit=3)
        for i in range(3):
            self.assertIn("N%d::hit" % i, text)
        self.assertNotIn("hitAdjacent", text)

    # ---- kind filtering ----
    def test_kind_filter_excludes_other_kinds(self):
        symbols = [self.sym("Thing", 5), self.sym("ThingMaker", 12)]
        text = self.search(symbols, "Thing", kind="class")
        self.assertIn("Thing", text)
        self.assertNotIn("ThingMaker", text)

    def test_kind_filter_miss_names_the_kinds_it_did_find(self):
        """Must not read as 'no such symbol' -- the name exists, the filter
        is what excluded it, and the caller needs to know which to fix."""
        text = self.search([self.sym("Backend", 6)], "Backend", kind="macro")
        self.assertIn("none of kind 'macro'", text)
        self.assertIn("method", text)

    def test_unknown_kind_is_rejected_not_silently_ignored(self):
        """Falling through to an unfiltered search would return results that
        disregard the constraint while looking like they honoured it."""
        text = self.search([self.sym("Thing", 5)], "Thing", kind="klass")
        self.assertIn("unknown kind", text)
        self.assertNotIn("Thing  ", text)

    def test_struct_filter_matches_the_kind_clangd_actually_reports(self):
        """Verified against the corpus: clangd files C++ structs under Class,
        so a spec-literal {23} filter would report zero structs in a repo
        full of them."""
        text = self.search([self.sym("Point", 5)], "Point", kind="struct")
        self.assertIn("Point", text)
        self.assertNotIn("none of kind", text)

    def test_unknown_kind_number_is_labelled_not_dropped(self):
        text = self.search([self.sym("Odd", 99)], "Odd")
        self.assertIn("Odd", text)
        self.assertIn("kind99", text)

    # ---- bounding ----
    def test_limit_truncates_and_reports_the_remainder(self):
        symbols = [self.sym("Sym%02d" % i) for i in range(20)]
        text = self.search(symbols, "Sym", limit=5)
        self.assertIn("15 more not shown", text)
        self.assertEqual(text.count("file:///repo") + text.count("a.cpp"), 5)

    def test_clangd_result_cap_is_disclosed(self):
        """At clangd's own cap the list was cut server-side; a caller reading
        it as complete is how 'that symbol does not exist' gets said wrongly."""
        symbols = [self.sym("Sym%03d" % i) for i in range(qe.CLANGD_RESULT_CAP)]
        text = self.search(symbols, "Sym")
        self.assertIn("not exhaustive", text)

    def test_limit_is_clamped_rather_than_crashing(self):
        symbols = [self.sym("Sym%02d" % i) for i in range(3)]
        for bad in (0, -5, "nonsense", None):
            text = self.search(symbols, "Sym", limit=bad)
            self.assertIn("Sym00", text, "limit=%r produced: %s" % (bad, text))

    # ---- misses ----
    def test_no_match_message_is_not_empty(self):
        text = self.search([], "nothing")
        self.assertTrue(text.strip(), "a miss returned an empty string")
        self.assertIn("nothing", text)

    def test_blank_query_is_rejected(self):
        for blank in ("", "   ", None, 12):
            text = self.search([self.sym("Thing")], blank)
            self.assertIn("non-empty query", text, "query=%r" % blank)

    def test_client_without_macro_fallback_still_answers(self):
        """FakeQueryClient has no resolve_macro; the fallback must degrade to
        a clean miss rather than an AttributeError."""
        text = self.search([], "SOME_MACRO")
        self.assertIn("no symbols matching", text)


class FakeOutlineClient:
    """A client that answers documentSymbol and nothing else."""
    def __init__(self, symbols=None, root="/repo", error=None):
        self._symbols = symbols
        self._error = error
        self.root = root

    def document_symbol(self, path, timeout=None):
        if self._error:
            raise RuntimeError(self._error)
        return self._symbols


class TestFileOutline(unittest.TestCase):
    def node(self, name, kind=6, start=0, end=0, detail="", children=None):
        node = {"name": name, "kind": kind, "detail": detail,
                "range": {"start": {"line": start}, "end": {"line": end}}}
        if children is not None:
            node["children"] = children
        return node

    def outline(self, symbols, **kwargs):
        client = FakeOutlineClient(symbols=symbols)
        out = io.StringIO()
        qe.run_outline_query(client, "a.cpp", out=out, **kwargs)
        return out.getvalue()

    def test_nested_symbols_are_indented(self):
        tree = [self.node("Cls", 5, 0, 9, children=[self.node("m", 6, 2, 4)])]
        lines = self.outline(tree).splitlines()
        parent = [l for l in lines if "Cls" in l][0]
        child = [l for l in lines if " m " in l][0]
        self.assertLess(len(parent) - len(parent.lstrip()),
                        len(child) - len(child.lstrip()),
                        "child not indented deeper:\n%s" % "\n".join(lines))

    def test_spans_are_one_based(self):
        """Reported line numbers must match what an editor shows, since the
        caller's next move is to read that range."""
        text = self.outline([self.node("f", 12, 9, 19)])
        self.assertIn("10-20", text)

    def test_signature_detail_is_carried(self):
        """The point of using detail: types without a second query."""
        text = self.outline([self.node("f", 12, 0, 0, detail="int (const Str &)")])
        self.assertIn("int (const Str &)", text)

    def test_detail_that_merely_repeats_the_kind_is_dropped(self):
        text = self.outline([self.node("Cls", 5, 0, 9, detail="class")])
        self.assertEqual(text.count("class"), 1, text)

    def test_flat_symbolinformation_shape_is_handled(self):
        """The reply shape is negotiated; a server that declines the
        hierarchical capability sends ranges nested under location."""
        flat = [{"name": "f", "kind": 12,
                 "location": {"uri": "file:///repo/a.cpp",
                              "range": {"start": {"line": 4}, "end": {"line": 6}}}}]
        self.assertIn("5-7", self.outline(flat))

    def test_empty_outline_is_explained_not_blank(self):
        text = self.outline([])
        self.assertIn("no symbols", text)
        self.assertIn("compile_commands.json", text)

    def test_limit_truncates_and_reports_the_remainder(self):
        text = self.outline([self.node("f%d" % i) for i in range(10)], limit=4)
        self.assertIn("6 more not shown", text)

    def test_malformed_nodes_do_not_crash(self):
        tree = [self.node("ok"), "junk", None,
                {"name": None, "kind": 6, "range": None}]
        text = self.outline(tree)
        self.assertIn("ok", text)
        self.assertIn("(unnamed)", text)

    def test_failure_is_reported_not_raised(self):
        client = FakeOutlineClient(error="clangd exited")
        out = io.StringIO()
        qe.run_outline_query(client, "a.cpp", out=out)
        self.assertIn("failed", out.getvalue())


class FakeDiagClient:
    """A client that answers the diagnostics() contract: (list, received)."""
    def __init__(self, diags=None, received=True, root="/repo", error=None):
        self._diags = diags or []
        self._received = received
        self._error = error
        self.root = root

    def diagnostics(self, path, timeout=15):
        if self._error:
            raise RuntimeError(self._error)
        return self._diags, self._received


class TestDiagnostics(unittest.TestCase):
    def diag(self, message="boom", severity=1, line=0, col=0,
             source="clang", code=None):
        return {"message": message, "severity": severity, "source": source,
                "code": code,
                "range": {"start": {"line": line, "character": col}}}

    def report(self, diags, received=True, **kwargs):
        client = FakeDiagClient(diags=diags, received=received)
        out = io.StringIO()
        qe.run_diagnostics_query(client, "a.cpp", out=out, **kwargs)
        return out.getvalue()

    def test_unreported_is_never_called_clean(self):
        """The failure this branch exists to prevent: clangd never answered,
        and the caller acts on a false all-clear."""
        text = self.report([], received=False)
        self.assertIn("NOT a clean bill of health", text)
        self.assertNotIn("parsed it cleanly", text)

    def test_clean_file_says_so_positively(self):
        text = self.report([], received=True)
        self.assertIn("parsed it cleanly", text)

    def test_counts_are_summarised(self):
        text = self.report([self.diag(severity=1), self.diag(severity=1),
                            self.diag(severity=2)])
        self.assertIn("2 errors, 1 warning", text)

    def test_severity_filter_keeps_errors_only(self):
        text = self.report([self.diag("bad", 1), self.diag("meh", 2)],
                           severity="error")
        self.assertIn("bad", text)
        self.assertNotIn("meh", text)

    def test_warning_filter_includes_errors(self):
        """'warning' means 'at least warning' -- dropping errors from a
        warning-level report would hide the more serious problem."""
        text = self.report([self.diag("bad", 1), self.diag("meh", 2)],
                           severity="warning")
        self.assertIn("bad", text)
        self.assertIn("meh", text)

    def test_filtered_to_nothing_still_reports_what_exists(self):
        """Must not read as 'file is clean' when it has warnings."""
        text = self.report([self.diag("meh", 2)], severity="error")
        self.assertIn("no diagnostics at severity", text)
        self.assertIn("1 warning", text)

    def test_unknown_severity_is_rejected(self):
        text = self.report([self.diag()], severity="critical")
        self.assertIn("unknown severity", text)

    def test_rows_are_one_based_and_ordered_by_line(self):
        text = self.report([self.diag("second", 1, line=9),
                            self.diag("first", 1, line=2)])
        rows = [l for l in text.splitlines() if not l.startswith("#")]
        self.assertIn("first", rows[0])
        self.assertIn(":3:1", rows[0])
        self.assertIn("second", rows[1])

    def test_source_and_code_are_tagged(self):
        text = self.report([self.diag(source="clang", code="undeclared_var_use")])
        self.assertIn("[clang:undeclared_var_use]", text)

    def test_missing_source_and_code_leave_no_empty_brackets(self):
        text = self.report([self.diag(source=None, code=None)])
        self.assertNotIn("[]", text)

    def test_multiline_message_is_flattened(self):
        """A raw newline would break the one-row-per-diagnostic format."""
        text = self.report([self.diag("line one\nline two")])
        self.assertIn("line one line two", text)

    def test_malformed_diagnostics_do_not_crash(self):
        text = self.report(["junk", None, {"message": "ok", "severity": 1}])
        self.assertIn("ok", text)

    def test_failure_is_reported_not_raised(self):
        client = FakeDiagClient(error="clangd exited")
        out = io.StringIO()
        qe.run_diagnostics_query(client, "a.cpp", out=out)
        self.assertIn("failed", out.getvalue())


class TestDocumentStaleness(unittest.TestCase):
    """A long-lived client outlives the files it reads. Without a staleness
    check every answer after an edit is computed from the contents at first
    open -- silently, and for the life of the process."""

    def client_recording_notifies(self):
        c = blank_client()
        sent = []
        c._notify = lambda method, params: sent.append((method, params))
        return c, sent

    def test_first_open_sends_did_open(self):
        c, sent = self.client_recording_notifies()
        with tempfile.NamedTemporaryFile("w", suffix=".cpp", delete=False) as f:
            f.write("int a;\n")
            path = f.name
        try:
            c.did_open(path)
            self.assertEqual([m for m, _ in sent], ["textDocument/didOpen"])
        finally:
            os.unlink(path)

    def test_unchanged_file_is_not_resent(self):
        c, sent = self.client_recording_notifies()
        with tempfile.NamedTemporaryFile("w", suffix=".cpp", delete=False) as f:
            f.write("int a;\n")
            path = f.name
        try:
            c.did_open(path)
            c.did_open(path)
            c.did_open(path)
            self.assertEqual(len(sent), 1, "re-sent an unchanged document")
        finally:
            os.unlink(path)

    def test_edited_file_is_pushed_as_did_change(self):
        """The regression this prevents: edit a file, re-query, and keep
        getting answers about the old contents."""
        c, sent = self.client_recording_notifies()
        with tempfile.NamedTemporaryFile("w", suffix=".cpp", delete=False) as f:
            f.write("int a;\n")
            path = f.name
        try:
            c.did_open(path)
            # bump mtime as well as contents; some filesystems have coarse
            # timestamps, so the size change is what makes this deterministic
            with open(path, "w", encoding="utf-8") as f:
                f.write("int a;\nint b;\nint c;\n")
            os.utime(path, (0, 0))
            c.did_open(path)

            self.assertEqual([m for m, _ in sent],
                             ["textDocument/didOpen", "textDocument/didChange"])
            change = sent[1][1]
            self.assertEqual(change["contentChanges"][0]["text"],
                             "int a;\nint b;\nint c;\n")
            self.assertGreater(change["textDocument"]["version"], 1,
                               "version must advance or clangd ignores the change")
        finally:
            os.unlink(path)

    def test_edit_invalidates_cached_diagnostics(self):
        """Diagnostics for the previous contents must not be handed back as
        if they described the new ones."""
        c, sent = self.client_recording_notifies()
        with tempfile.NamedTemporaryFile("w", suffix=".cpp", delete=False) as f:
            f.write("int a;\n")
            path = f.name
        try:
            uri = c._uri(path)
            c.did_open(path)
            c._store_diagnostics({"uri": uri, "diagnostics": [{"message": "old"}]})
            self.assertTrue(c._diag_events[uri].is_set())

            with open(path, "w", encoding="utf-8") as f:
                f.write("int a;\nint b;\n")
            os.utime(path, (0, 0))
            c.did_open(path)

            self.assertNotIn(uri, c._diagnostics, "stale diagnostics survived an edit")
            self.assertFalse(c._diag_events[uri].is_set())
        finally:
            os.unlink(path)


class TestDiagnosticsCapture(unittest.TestCase):
    """publishDiagnostics is unsolicited -- there is no request to correlate
    it with, so it has to be caught as it arrives."""

    def test_notification_is_stored_and_signalled(self):
        c = blank_client()
        c._store_diagnostics({"uri": "file:///repo/a.cpp",
                              "diagnostics": [{"message": "boom"}]})
        self.assertEqual(c._diagnostics["file:///repo/a.cpp"][0]["message"], "boom")
        self.assertTrue(c._diag_events["file:///repo/a.cpp"].is_set())

    def test_empty_list_still_counts_as_received(self):
        """An empty publish is how clangd says 'clean' -- treating it as
        'nothing arrived' would turn every clean file into a timeout."""
        c = blank_client()
        c._store_diagnostics({"uri": "file:///repo/a.cpp", "diagnostics": []})
        self.assertTrue(c._diag_events["file:///repo/a.cpp"].is_set())
        self.assertEqual(c._diagnostics["file:///repo/a.cpp"], [])

    def test_notification_without_uri_is_ignored(self):
        c = blank_client()
        c._store_diagnostics({"diagnostics": []})
        self.assertEqual(c._diagnostics, {})

    def test_null_diagnostics_becomes_a_list(self):
        c = blank_client()
        c._store_diagnostics({"uri": "file:///repo/a.cpp", "diagnostics": None})
        self.assertEqual(c._diagnostics["file:///repo/a.cpp"], [])

    def test_reader_routes_publish_diagnostics(self):
        """End to end through the transport: a framed notification must reach
        the store, not be dropped as an unknown method."""
        note = framed({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                       "params": {"uri": "file:///repo/a.cpp",
                                  "diagnostics": [{"message": "boom", "severity": 1}]}})
        c = blank_client()
        c.proc = fake_proc(stream=note)
        c._reader()
        self.assertIn("file:///repo/a.cpp", c._diagnostics)
