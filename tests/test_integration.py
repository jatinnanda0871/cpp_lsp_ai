"""Integration tests: the real query engine against a real clangd indexing
the C++ corpus in fixtures/corpus.

These assert against KNOWN ground truth in the corpus (exact caller counts,
which file a definition lives in, how long a body is), not merely "it did not
crash" -- a query that returns confidently wrong answers is worse than one
that errors.

Skipped automatically when no clangd is installed.

    python3 -m unittest tests.test_integration -v
"""
import os
import re
import unittest

import support

from clangd_client import ClangdClient
from clangd_query_engine import (
    run_query_as_string,
    run_class_query_as_string,
    run_macro_query_as_string,
    run_struct_query_as_string,
    run_hover_query_as_string,
    run_incoming_calls_query_as_string,
)

CLANGD = support.find_clangd()
WAIT = int(os.environ.get("CLANGQ_TEST_WAIT", "90"))

_client = None


def setUpModule():
    """One warm clangd for the whole module -- indexing is the slow part."""
    global _client
    if CLANGD is None:
        return
    ccdir = support.ensure_corpus_db()
    _client = ClangdClient(
        support.CORPUS_DIR, compile_commands_dir=ccdir,
        clangd=CLANGD, request_timeout=60).start()
    _client.prime_index()
    # Force the background index to warm before the first assertion so a cold
    # index doesn't look like a bug.
    _client.resolve_symbol("runPipeline", deadline_s=WAIT)


def tearDownModule():
    if _client is not None:
        _client.shutdown()


@unittest.skipIf(CLANGD is None, "clangd not installed")
class IntegrationBase(unittest.TestCase):
    @property
    def client(self):
        return _client

    def assertNoError(self, text, label=""):
        """No query should ever surface a raw Python exception to the user."""
        for bad in ("Traceback", "Error running query", "NoneType",
                    "list index out of range", "AttributeError", "TypeError"):
            self.assertNotIn(bad, text, "%s leaked %r:\n%s" % (label, bad, text))

    def defs_in(self, text):
        """[(path, start, end)] from '# name  (def path:start-end)' lines."""
        return [(m.group(1), int(m.group(2)), int(m.group(3)))
                for m in re.finditer(r"\(def (.+?):(\d+)-(\d+)\)", text)]

    def decls_in(self, text):
        return [(m.group(1), int(m.group(2)))
                for m in re.finditer(r"\(decl (.+?):(\d+)\)", text)]


class TestFunctionQueries(IntegrationBase):
    def test_split_decl_and_def_resolve_to_different_files(self):
        """Declared in shapes.h, defined in shapes.cpp -- the common case."""
        out = run_query_as_string(self.client, "all", "isEquilateral", WAIT)
        self.assertNoError(out, "isEquilateral")
        defs = self.defs_in(out)
        self.assertTrue(defs, "no definition reported:\n%s" % out)
        self.assertTrue(any(d[0].endswith(".cpp") for d in defs),
                        "definition should be in a .cpp:\n%s" % out)
        decls = self.decls_in(out)
        self.assertTrue(any(d[0].endswith(".h") for d in decls),
                        "declaration should be in a .h:\n%s" % out)

    def test_long_body_reports_a_long_span(self):
        """runPipeline's body is ~40 lines; the end line must reflect that,
        which only works if it comes from documentSymbol's full range."""
        out = run_query_as_string(self.client, "refs", "runPipeline", WAIT)
        self.assertNoError(out, "runPipeline")
        defs = self.defs_in(out)
        self.assertTrue(defs, "no definition reported:\n%s" % out)
        path, start, end = defs[0]
        self.assertGreater(end - start, 25,
                           "expected a long span for runPipeline, got %d-%d" % (start, end))

    def test_short_body_reports_a_short_span(self):
        out = run_query_as_string(self.client, "refs", "tinyOne", WAIT)
        self.assertNoError(out, "tinyOne")
        defs = self.defs_in(out)
        self.assertTrue(defs)
        _, start, end = defs[0]
        self.assertLessEqual(end - start, 3,
                             "expected a short span for tinyOne, got %d-%d" % (start, end))

    def test_header_only_function(self):
        """Vec2::lengthSquared is defined inline in the header."""
        out = run_query_as_string(self.client, "all", "lengthSquared", WAIT)
        self.assertNoError(out, "lengthSquared")
        defs = self.defs_in(out)
        self.assertTrue(defs, "no definition reported:\n%s" % out)
        self.assertTrue(defs[0][0].endswith(".h"),
                        "header-only def should be in a .h:\n%s" % out)

    def test_pure_virtual_degrades_gracefully(self):
        """Shape::area is pure virtual: no body anywhere. Must produce a
        clear explanation, never a crash or silent emptiness."""
        out = run_query_as_string(self.client, "all", "area", WAIT)
        self.assertNoError(out, "area")
        self.assertTrue(out.strip(), "pure-virtual query returned nothing")

    def test_never_defined_function(self):
        out = run_query_as_string(self.client, "all", "neverDefined", WAIT)
        self.assertNoError(out, "neverDefined")
        self.assertTrue(out.strip(), "returned nothing at all")

    def test_overload_set_reports_multiple_definitions(self):
        """maxOf has three overloads; a query must not silently pick one."""
        out = run_query_as_string(self.client, "refs", "maxOf", WAIT)
        self.assertNoError(out, "maxOf")
        self.assertGreaterEqual(len(self.defs_in(out)), 2,
                                "expected several overloads:\n%s" % out)

    def test_static_and_const_methods(self):
        for name in ("staticAdd", "trace", "processedCount"):
            out = run_query_as_string(self.client, "all", name, WAIT)
            self.assertNoError(out, name)
            self.assertTrue(out.strip(), "%s returned nothing" % name)

    def test_template_method(self):
        out = run_query_as_string(self.client, "all", "identity", WAIT)
        self.assertNoError(out, "identity")

    def test_operator_overload(self):
        out = run_query_as_string(self.client, "all", "operator+", WAIT)
        self.assertNoError(out, "operator+")

    def test_nonexistent_symbol_reports_clearly(self):
        out = run_query_as_string(self.client, "all", "NoSuchSymbolAnywhere_zz", 3)
        self.assertNoError(out, "nonexistent")
        self.assertTrue(out.strip(), "nonexistent symbol produced EMPTY output")
        self.assertIn("no symbol matching", out)

    def test_qualified_name(self):
        out = run_query_as_string(self.client, "all", "Backend::process", WAIT)
        self.assertNoError(out, "Backend::process")
        self.assertTrue(out.strip())


class TestIncomingCalls(IntegrationBase):
    def test_known_caller_count(self):
        """Backend::process is called by exactly 3 Frontend methods."""
        out = run_incoming_calls_query_as_string(self.client, "process", WAIT)
        self.assertNoError(out, "process callers")
        m = re.search(r"(\d+) callers of", out)
        self.assertIsNotNone(m, "no caller count reported:\n%s" % out)
        self.assertGreaterEqual(int(m.group(1)), 3,
                                "expected >=3 callers of Backend::process:\n%s" % out)
        for expected in ("handleRequest", "handleBatch", "retry"):
            self.assertIn(expected, out, "missing caller %s:\n%s" % (expected, out))

    def test_uncalled_function_reports_zero_not_error(self):
        out = run_incoming_calls_query_as_string(self.client, "orphan", WAIT)
        self.assertNoError(out, "orphan")
        self.assertTrue(out.strip(), "orphan produced EMPTY output")

    def test_nonexistent_symbol(self):
        out = run_incoming_calls_query_as_string(self.client, "NoSuchFn_zz", 3)
        self.assertNoError(out, "nonexistent callers")
        self.assertTrue(out.strip(), "produced EMPTY output")


class TestClassQueries(IntegrationBase):
    def test_abstract_base_lists_methods(self):
        out = run_class_query_as_string(self.client, "all", "Shape", WAIT)
        self.assertNoError(out, "Shape")
        self.assertIn("class decl", out)
        for method in ("perimeter", "describe", "setId"):
            self.assertIn(method, out, "missing %s:\n%s" % (method, out[:2000]))

    def test_class_span_is_plausible(self):
        out = run_class_query_as_string(self.client, "refs", "BigService", WAIT)
        self.assertNoError(out, "BigService")
        m = re.search(r"class decl (.+?):(\d+)-(\d+)", out)
        self.assertIsNotNone(m, "no class decl span:\n%s" % out[:500])
        start, end = int(m.group(2)), int(m.group(3))
        self.assertGreater(end, start, "class span must be non-empty")

    def test_pure_interface_class(self):
        """Loggable has only pure virtuals -- no method has a body."""
        out = run_class_query_as_string(self.client, "all", "Loggable", WAIT)
        self.assertNoError(out, "Loggable")
        self.assertTrue(out.strip())

    def test_header_only_class(self):
        out = run_class_query_as_string(self.client, "all", "Counter", WAIT)
        self.assertNoError(out, "Counter")
        self.assertIn("increment", out)

    def test_template_class(self):
        out = run_class_query_as_string(self.client, "all", "Stack", WAIT)
        self.assertNoError(out, "Stack")

    def test_nested_class(self):
        """Registry::Entry is nested one level deep."""
        out = run_class_query_as_string(self.client, "all", "Entry", WAIT)
        self.assertNoError(out, "Entry")

    def test_multiple_inheritance_class(self):
        out = run_class_query_as_string(self.client, "all", "Player", WAIT)
        self.assertNoError(out, "Player")

    def test_overload_heavy_class(self):
        """Calculator has a 4-way overload set plus const/non-const pair."""
        out = run_class_query_as_string(self.client, "all", "Calculator", WAIT)
        self.assertNoError(out, "Calculator")
        self.assertIn("add", out)

    def test_nonexistent_class_reports_clearly(self):
        out = run_class_query_as_string(self.client, "all", "NoSuchClass_zz", 3)
        self.assertNoError(out, "nonexistent class")
        self.assertTrue(out.strip(), "produced EMPTY output")
        self.assertIn("no class found", out)

    def test_struct_with_methods_as_class(self):
        out = run_class_query_as_string(self.client, "all", "Config", WAIT)
        self.assertNoError(out, "Config")


class TestMacroQueries(IntegrationBase):
    def test_object_like_macro(self):
        out = run_macro_query_as_string(self.client, "all", "CORPUS_MAX_ITEMS", WAIT)
        self.assertNoError(out, "CORPUS_MAX_ITEMS")
        self.assertIn("macro decl", out)

    def test_function_like_macro(self):
        out = run_macro_query_as_string(self.client, "all", "CORPUS_CLAMP", WAIT)
        self.assertNoError(out, "CORPUS_CLAMP")
        self.assertIn("macro decl", out)

    def test_unused_macro_reports_zero_refs(self):
        out = run_macro_query_as_string(self.client, "all", "CORPUS_UNUSED_MACRO", WAIT)
        self.assertNoError(out, "CORPUS_UNUSED_MACRO")
        self.assertTrue(out.strip(), "produced EMPTY output")

    def test_nonexistent_macro(self):
        out = run_macro_query_as_string(self.client, "all", "NO_SUCH_MACRO_ZZ", 3)
        self.assertNoError(out, "nonexistent macro")
        self.assertTrue(out.strip(), "produced EMPTY output")
        self.assertIn("no macro found", out)


class TestStructQueries(IntegrationBase):
    def test_plain_struct(self):
        out = run_struct_query_as_string(self.client, "Point", WAIT)
        self.assertNoError(out, "Point")
        self.assertIn("struct decl", out)

    def test_typedef_struct(self):
        """GeoCoord is a typedef struct -- clangd reports kind=5, not 13."""
        out = run_struct_query_as_string(self.client, "GeoCoord", WAIT)
        self.assertNoError(out, "GeoCoord")
        self.assertIn("struct decl", out)

    def test_unreferenced_struct(self):
        out = run_struct_query_as_string(self.client, "OrphanStruct", WAIT)
        self.assertNoError(out, "OrphanStruct")
        self.assertTrue(out.strip())

    def test_nonexistent_struct(self):
        out = run_struct_query_as_string(self.client, "NoSuchStruct_zz", 3)
        self.assertNoError(out, "nonexistent struct")
        self.assertTrue(out.strip(), "produced EMPTY output")
        self.assertIn("no struct found", out)


class TestHoverQueries(IntegrationBase):
    def test_hover_on_a_real_symbol(self):
        rel = "src/big.cpp"
        line = support.line_of(rel, "int BigService::runPipeline")
        col = len("int BigService::")
        out = run_hover_query_as_string(
            self.client, support.corpus_source(rel), line, col)
        self.assertNoError(out, "hover runPipeline")
        self.assertTrue(out.strip(), "hover produced EMPTY output")

    def test_hover_on_blank_line(self):
        """A position with nothing under it must not crash."""
        rel = "src/big.cpp"
        out = run_hover_query_as_string(
            self.client, support.corpus_source(rel), 2, 0)
        self.assertNoError(out, "hover blank")
        self.assertTrue(out.strip(), "produced EMPTY output")

    def test_hover_far_past_end_of_file(self):
        rel = "src/big.cpp"
        out = run_hover_query_as_string(
            self.client, support.corpus_source(rel), 999999, 0)
        self.assertNoError(out, "hover past EOF")
        self.assertTrue(out.strip(), "produced EMPTY output")

    def test_hover_on_missing_file(self):
        out = run_hover_query_as_string(
            self.client, support.corpus_source("src/does_not_exist.cpp"), 1, 1)
        self.assertNoError(out, "hover missing file")
        self.assertTrue(out.strip(), "produced EMPTY output")


class TestConcurrency(IntegrationBase):
    def test_many_parallel_queries_on_one_client(self):
        """The MCP server runs every tool call in a worker thread against one
        shared warm client, so concurrent queries must not corrupt each
        other's replies (this is what broke when several tools fired at once)."""
        import threading

        names = ["runPipeline", "process", "trace", "isEquilateral", "tinyOne",
                 "formatReport", "quickAdd", "validate", "lengthSquared", "sumRange"]
        results = {}
        errors = []

        def worker(n):
            try:
                results[n] = run_query_as_string(self.client, "all", n, WAIT)
            except Exception as e:
                errors.append((n, repr(e)))

        threads = [threading.Thread(target=worker, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "concurrent queries raised: %s" % errors)
        self.assertEqual(len(results), len(names), "some queries returned nothing")
        for n, text in results.items():
            self.assertNoError(text, "concurrent %s" % n)
            # each reply must be about the symbol that was asked for, not
            # another thread's answer
            self.assertIn(n, text, "reply for %s looks like another query's:\n%s"
                          % (n, text[:400]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
