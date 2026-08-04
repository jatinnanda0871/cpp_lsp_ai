"""Negative / hostile-input tests.

Every case here is something a caller can actually send: a mistyped symbol, a
wrong argument type, a root that does not exist, a malformed request object.
The contract under test is narrow but strict:

  1. never raise out of `_run_tool` -- an exception escaping the worker thread
     propagates into the MCP session rather than being reported to the caller;
  2. never return an empty string -- "" tells the assistant nothing;
  3. never answer confidently when the match was only approximate;
  4. never blame the user's spelling on a cold index, or vice versa.

The pure-argument tests need no clangd. The ones that reach clangd are marked
and skip without it.

    python3 -m unittest tests.test_negative -v
"""
import os
import tempfile
import unittest

import support

import clangd_query_engine as qe

try:
    import clangq_mcp
    MCP_AVAILABLE = True
except Exception:                # pragma: no cover - depends on environment
    MCP_AVAILABLE = False

CLANGD = support.find_clangd()

# Text that must never reach a caller: raw Python failures leaking through.
LEAKED = ("Traceback", "NoneType", "AttributeError", "TypeError",
          "list index out of range", "KeyError", "-32602")


class StubClient:
    """A client that satisfies _run_tool without starting clangd."""
    _db_found = True
    root = "/repo"

    def is_alive(self):
        return True


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class TestMalformedRequests(unittest.TestCase):
    """Argument validation, all before clangd is ever contacted."""

    def setUp(self):
        self._real = clangq_mcp.get_client
        clangq_mcp.get_client = lambda root: StubClient()
        self.root = support.CORPUS_DIR   # a real directory, so root passes

    def tearDown(self):
        clangq_mcp.get_client = self._real

    def run_tool(self, tool, args):
        out = clangq_mcp._run_tool(tool, args)
        self.assertIsInstance(out, str, "%s did not return a string" % tool)
        self.assertTrue(out.strip(), "%s returned an EMPTY string" % tool)
        for bad in LEAKED:
            self.assertNotIn(bad, out, "%s leaked %r:\n%s" % (tool, bad, out))
        return out

    # ---- the request object itself ----
    def test_arguments_is_none(self):
        """A null argument object used to raise AttributeError straight out of
        the worker thread instead of being reported."""
        self.assertIn("Error", self.run_tool("get_function_info", None))

    def test_arguments_is_a_list(self):
        self.assertIn("must be an object", self.run_tool("get_function_info", []))

    def test_arguments_is_a_string(self):
        self.assertIn("must be an object", self.run_tool("get_function_info", "oops"))

    def test_arguments_is_an_int(self):
        self.assertIn("must be an object", self.run_tool("get_function_info", 7))

    def test_unknown_tool_name(self):
        self.assertIn("Unknown tool",
                      self.run_tool("get_everything", {"root": self.root}))

    # ---- root ----
    def test_root_missing(self):
        self.assertIn("root is required", self.run_tool("get_function_info", {}))

    def test_root_empty_string(self):
        """A blank string means "not supplied", so it gets the same message as
        an omitted argument rather than a complaint about its type."""
        self.assertIn("root is required",
                      self.run_tool("get_function_info", {"root": ""}))

    def test_root_wrong_type(self):
        out = self.run_tool("get_function_info", {"root": 123, "name": "x"})
        self.assertIn("root", out)

    def test_root_does_not_exist(self):
        missing = os.path.join(tempfile.gettempdir(), "no_such_repo_zz9")
        out = self.run_tool("get_function_info", {"root": missing, "name": "x"})
        self.assertIn("does not exist", out)

    def test_root_is_a_file_not_a_directory(self):
        """Relativising paths against a file produced output like '(def .:171)'."""
        out = self.run_tool("get_function_info",
                            {"root": support.corpus_source("src/big.cpp"), "name": "x"})
        self.assertIn("not a directory", out)

    # ---- name ----
    def test_name_missing(self):
        for tool in ("get_function_info", "get_class_info", "get_macro_info",
                     "get_struct_info", "get_incoming_calls"):
            self.assertIn("name is required",
                          self.run_tool(tool, {"root": self.root}))

    def test_name_is_none(self):
        self.assertIn("name is required",
                      self.run_tool("get_class_info", {"root": self.root, "name": None}))

    def test_name_is_empty_or_blank(self):
        for value in ("", "   ", "\t\n"):
            self.assertIn("name is required",
                          self.run_tool("get_class_info",
                                        {"root": self.root, "name": value}))

    def test_name_wrong_types(self):
        """A non-string went to clangd as-is and came back as a raw JSON-RPC
        -32602, which names neither the argument nor the problem."""
        for value in (123, ["a"], {"a": 1}, True, 1.5):
            out = self.run_tool("get_function_info",
                                {"root": self.root, "name": value})
            self.assertIn("name must be a string", out)

    # ---- mode ----
    def test_invalid_mode_is_rejected(self):
        """An unknown mode used to be accepted silently: no section matched, so
        the caller got a definition line and simply no refs/callers."""
        for value in ("bogus", "REFS", "", 123, ["refs"], {}):
            out = self.run_tool("get_function_info",
                                {"root": self.root, "name": "x", "mode": value})
            self.assertIn("mode must be one of", out)

    def test_omitted_mode_is_allowed(self):
        out = self.run_tool("get_function_info", {"root": self.root, "name": "x"})
        self.assertNotIn("mode must be one of", out)

    def test_explicit_null_mode_is_allowed(self):
        out = self.run_tool("get_function_info",
                            {"root": self.root, "name": "x", "mode": None})
        self.assertNotIn("mode must be one of", out)

    def test_every_valid_mode_is_accepted(self):
        for value in ("refs", "callers", "all"):
            out = self.run_tool("get_function_info",
                                {"root": self.root, "name": "x", "mode": value})
            self.assertNotIn("mode must be one of", out)

    # ---- hover coordinates ----
    def test_hover_missing_path(self):
        out = self.run_tool("get_hover_info",
                            {"root": self.root, "line": 1, "col": 1})
        self.assertIn("path is required", out)

    def test_hover_path_wrong_type(self):
        out = self.run_tool("get_hover_info",
                            {"root": self.root, "path": 42, "line": 1, "col": 1})
        self.assertIn("path must be a string", out)

    def test_hover_null_coordinates(self):
        """The originally reported crash: line=None reached 'line + 1'."""
        for line, col in ((None, 1), (1, None), (None, None)):
            out = self.run_tool("get_hover_info",
                                {"root": self.root, "path": "a.cpp",
                                 "line": line, "col": col})
            self.assertIn("is required", out)

    def test_hover_string_coordinates(self):
        out = self.run_tool("get_hover_info",
                            {"root": self.root, "path": "a.cpp", "line": "12", "col": 0})
        self.assertIn("must be an integer", out)

    def test_hover_float_coordinates(self):
        out = self.run_tool("get_hover_info",
                            {"root": self.root, "path": "a.cpp", "line": 1.5, "col": 0})
        self.assertIn("must be an integer", out)

    def test_hover_bool_coordinates(self):
        """bool subclasses int, so isinstance(True, int) passes -- `true` used
        to be silently accepted as line 1."""
        out = self.run_tool("get_hover_info",
                            {"root": self.root, "path": "a.cpp", "line": True, "col": 0})
        self.assertIn("must be an integer", out)

    def test_hover_negative_coordinates(self):
        for line, col in ((-1, 0), (0, -1)):
            out = self.run_tool("get_hover_info",
                                {"root": self.root, "path": "a.cpp",
                                 "line": line, "col": col})
            self.assertIn("must not be negative", out)


class TestInexactMatchReporting(unittest.TestCase):
    """workspace/symbol is a FUZZY search, so it answers even for nonsense.
    Reporting that answer in the same format as a real hit made a guess look
    like a fact -- a query for '::' came back with an unrelated struct."""

    def sym(self, name, container=None, kind=12):
        s = {"name": name, "kind": kind,
             "location": {"uri": "file:///r/a.cpp",
                          "range": {"start": {"line": 0, "character": 0}}}}
        if container is not None:
            s["containerName"] = container
        return s

    def test_exact_name_matches(self):
        syms = [self.sym("Foo"), self.sym("FooBar")]
        self.assertEqual(len(qe.exact_matches(syms, "Foo")), 1)

    def test_case_difference_is_not_exact(self):
        self.assertEqual(qe.exact_matches([self.sym("Shape", kind=5)], "shape"), [])

    def test_qualified_name_still_counts_as_exact(self):
        """clangd reports Class::method as name='method' + containerName, so a
        plain string compare would call every qualified lookup a fuzzy guess."""
        syms = [self.sym("process", container="Backend", kind=6)]
        self.assertEqual(len(qe.exact_matches(syms, "Backend::process")), 1)

    def test_qualified_name_with_namespace(self):
        syms = [self.sym("process", container="chain::Backend", kind=6)]
        self.assertEqual(len(qe.exact_matches(syms, "Backend::process")), 1)

    def test_qualified_name_wrong_container_is_not_exact(self):
        syms = [self.sym("process", container="Other", kind=6)]
        self.assertEqual(qe.exact_matches(syms, "Backend::process"), [])

    def test_kind_filter_applies(self):
        syms = [self.sym("Shape", kind=12)]
        self.assertEqual(qe.exact_matches(syms, "Shape", kind=5), [])
        self.assertEqual(len(qe.exact_matches(syms, "Shape", kind=12)), 1)

    def test_non_string_name_matches_nothing(self):
        self.assertEqual(qe.exact_matches([self.sym("Foo")], 123), [])

    def test_note_names_the_query_and_the_matches(self):
        note = qe.inexact_match_note([self.sym("Config"), self.sym("Point")], "::")
        self.assertIn("::", note)
        self.assertIn("Config", note)

    def test_pick_symbol_kind_still_falls_back(self):
        """The fuzzy fallback is deliberate -- near-misses should still
        resolve. Only the silent presentation was the problem."""
        syms = [self.sym("Config", kind=5)]
        self.assertIsNotNone(qe.pick_symbol_kind(syms, "::", 5))

    def test_pick_symbol_kind_on_empty(self):
        self.assertIsNone(qe.pick_symbol_kind([], "x", 5))


@unittest.skipIf(CLANGD is None, "clangd not installed")
@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class TestHostileInputAgainstRealClangd(unittest.TestCase):
    """The same hostile strings, but actually sent to clangd. None may crash,
    hang past the deadline, or come back empty."""

    ROOT = support.CORPUS_DIR

    @classmethod
    def setUpClass(cls):
        support.ensure_corpus_db()
        from clangd_client import ClangdClient
        ccdir = support.CORPUS_BUILD_DIR

        def factory(root):
            return ClangdClient(root, compile_commands_dir=ccdir,
                                clangd=CLANGD, request_timeout=30)

        cls._real_cls = clangq_mcp.ClangdClient
        clangq_mcp.ClangdClient = factory
        clangq_mcp._clients.clear()
        clangq_mcp._client_locks.clear()
        # warm the index so misses fail fast instead of polling
        clangq_mcp._run_tool("get_function_info", {"root": cls.ROOT, "name": "tinyOne"})

    @classmethod
    def tearDownClass(cls):
        for c in list(clangq_mcp._clients.values()):
            try:
                c.shutdown()
            except Exception:
                pass
        clangq_mcp._clients.clear()
        clangq_mcp.ClangdClient = cls._real_cls

    HOSTILE = [
        ("whitespace only", "   \t "),
        ("very long", "A" * 5000),
        ("regex metachars", ".*+[](){}|^$"),
        ("backslashes", "a\\b\\c"),
        ("unicode", "\u51fd\u6570\u540d"),
        ("emoji", "\U0001F600ok"),
        ("embedded newline", "foo\nbar"),
        ("null byte", "foo\x00bar"),
        ("shell metachars", "$(whoami); rm -rf /"),
        ("path traversal", "../../../etc/passwd"),
        ("quotes", "\"';--"),
        ("just colons", "::"),
        ("angle brackets", "Vec<int>"),
        ("digits", "12345"),
        ("nonexistent qualified", "NoSuch::method"),
    ]

    def check(self, tool, name):
        out = clangq_mcp._run_tool(tool, {"root": self.ROOT, "name": name})
        self.assertIsInstance(out, str)
        self.assertTrue(out.strip(), "%s(%r) returned EMPTY" % (tool, name))
        for bad in LEAKED:
            self.assertNotIn(bad, out, "%s(%r) leaked %r:\n%s" % (tool, name, bad, out))
        return out

    def test_function_query_survives_hostile_names(self):
        for label, value in self.HOSTILE:
            with self.subTest(label):
                self.check("get_function_info", value)

    def test_class_query_survives_hostile_names(self):
        for label, value in self.HOSTILE:
            with self.subTest(label):
                self.check("get_class_info", value)

    def test_macro_query_survives_hostile_names(self):
        for label, value in self.HOSTILE:
            with self.subTest(label):
                self.check("get_macro_info", value)

    def test_struct_query_survives_hostile_names(self):
        for label, value in self.HOSTILE:
            with self.subTest(label):
                self.check("get_struct_info", value)

    def test_incoming_calls_survives_hostile_names(self):
        for label, value in self.HOSTILE:
            with self.subTest(label):
                self.check("get_incoming_calls", value)

    def test_nonsense_name_is_flagged_as_inexact(self):
        """'::' fuzzy-matches something; the answer must say so."""
        out = self.check("get_function_info", "::")
        self.assertIn("note:", out)
        self.assertIn("exactly", out)

    def test_wrong_case_class_is_flagged_as_inexact(self):
        out = self.check("get_class_info", "shape")
        self.assertIn("note:", out)

    def test_exact_match_carries_no_inexact_note(self):
        out = self.check("get_class_info", "Shape")
        self.assertNotIn("nothing is named", out)

    def test_reported_name_is_the_symbol_found_not_the_query(self):
        """A fuzzy struct hit used to be printed under the REQUESTED name,
        asserting a symbol exists that does not."""
        out = self.check("get_struct_info", "point")
        self.assertIn("Point", out)
        self.assertNotIn("# point (struct decl", out)

    def test_hover_on_bad_coordinates_against_real_files(self):
        big = support.corpus_source("src/big.cpp")
        for line, col in ((10 ** 9, 0), (0, 10 ** 9)):
            out = clangq_mcp._run_tool(
                "get_hover_info",
                {"root": self.ROOT, "path": big, "line": line, "col": col})
            self.assertTrue(out.strip())
            for bad in LEAKED:
                self.assertNotIn(bad, out)

    def test_hover_on_unopenable_paths(self):
        for path in (support.CORPUS_DIR,                       # a directory
                     tempfile.gettempdir(),                    # outside the root
                     support.corpus_source("src/nope_zz.cpp")):  # missing
            out = clangq_mcp._run_tool(
                "get_hover_info", {"root": self.ROOT, "path": path, "line": 0, "col": 0})
            self.assertTrue(out.strip(), "hover on %r returned EMPTY" % path)
            for bad in LEAKED:
                self.assertNotIn(bad, out, "hover on %r leaked %r" % (path, bad))

    def test_macro_usage_limitation_is_stated(self):
        """clangd does not index macro expansion sites, so the reference list
        is the definition only. Printing that count bare reads as 'unused'."""
        out = self.check("get_macro_info", "CORPUS_MAX_ITEMS")
        self.assertIn("expansion sites", out,
                      "macro answer did not disclose the indexing limitation:\n%s" % out)

    def test_a_miss_does_not_take_the_full_deadline(self):
        """A warm index plus a bad name must fail fast; the MCP layer passes a
        120s deadline that would otherwise pin a worker thread."""
        import time
        t0 = time.time()
        self.check("get_function_info", "NoSuchSymbolAtAll_zz9")
        elapsed = time.time() - t0
        self.assertLess(elapsed, 30, "a warm miss took %.1fs" % elapsed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
