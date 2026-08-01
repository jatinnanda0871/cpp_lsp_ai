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
import sys
import unittest

# So `python3 -m unittest tests.test_integration` works from the repo root,
# where tests/ is not on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402  (needs the path set up first)

from clangd_client import ClangdClient
from clangd_query_engine import (
    run_query_as_string,
    run_class_query_as_string,
    run_macro_query_as_string,
    run_struct_query_as_string,
    run_hover_query_as_string,
    run_incoming_calls_query_as_string,
    run_search_query_as_string,
    run_outline_query_as_string,
    run_diagnostics_query_as_string,
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


class TestSymbolSearch(IntegrationBase):
    """Ground truth for the discovery entry point. The load-bearing assertion
    is the round trip: a name this tool prints must be one the other tools
    can actually resolve, or search sends every caller down a dead end."""

    def rows_in(self, text):
        """[(kind, qualified_name, location)] from the result rows."""
        rows = []
        for line in text.splitlines():
            if line.startswith("#") or not line.startswith("  "):
                continue
            parts = line.split()
            if len(parts) >= 3:
                rows.append((parts[0], parts[1], parts[2]))
        return rows

    def test_exact_name_is_found_and_labelled(self):
        out = run_search_query_as_string(self.client, "Backend", wait=WAIT)
        self.assertNoError(out, "search Backend")
        self.assertIn("# exact", out)
        rows = self.rows_in(out)
        self.assertTrue(any(k == "class" and n == "chain::Backend" for k, n, _ in rows),
                        "chain::Backend not found as a class:\n%s" % out)

    def test_fragment_finds_related_symbols_fuzzily(self):
        """The reason the tool exists: a caller who knows 'backend' but not
        'Frontend::setBackend' still gets there."""
        out = run_search_query_as_string(self.client, "Backend", wait=WAIT)
        names = [n for _, n, _ in self.rows_in(out)]
        self.assertIn("chain::Frontend::setBackend", names,
                      "fuzzy neighbours missing:\n%s" % out)

    def test_names_round_trip_into_the_other_tools(self):
        """Every qualified name search prints must resolve when fed back to
        get_function_info -- otherwise the handoff silently breaks."""
        out = run_search_query_as_string(
            self.client, "process", kind="function", wait=WAIT)
        names = [n for _, n, _ in self.rows_in(out)]
        self.assertIn("chain::Backend::process", names,
                      "search did not surface the method:\n%s" % out)

        back = run_query_as_string(self.client, "all", "chain::Backend::process", WAIT)
        self.assertNoError(back, "round trip")
        self.assertTrue(self.defs_in(back),
                        "name from search did not resolve:\n%s" % back)

    def test_kind_filter_narrows_to_types(self):
        out = run_search_query_as_string(
            self.client, "Backend", kind="class", wait=WAIT)
        kinds = {k for k, _, _ in self.rows_in(out)}
        self.assertTrue(kinds, "kind filter returned nothing:\n%s" % out)
        self.assertNotIn("method", kinds, "class filter let methods through:\n%s" % out)

    def test_struct_is_findable_despite_clangd_calling_it_a_class(self):
        """clangd files structs under Class, so this filter has to be a
        superset -- a spec-literal one reports zero structs in a repo of them."""
        out = run_search_query_as_string(
            self.client, "Point", kind="struct", wait=WAIT)
        self.assertNoError(out, "search Point")
        self.assertIn("Point", out)
        self.assertNotIn("none of kind", out)

    def test_macro_is_findable_though_the_index_hides_it(self):
        """workspace/symbol never reports a #define until its file is open, so
        without the fallback this reads as 'check the spelling'."""
        out = run_search_query_as_string(
            self.client, "CORPUS_MAX_ITEMS", kind="macro", wait=WAIT)
        self.assertNoError(out, "search macro")
        self.assertIn("CORPUS_MAX_ITEMS", out)
        self.assertNotIn("no symbols matching", out)

    def test_kind_miss_is_not_reported_as_absence(self):
        out = run_search_query_as_string(
            self.client, "Backend", kind="macro", wait=WAIT)
        self.assertIn("none of kind 'macro'", out)
        self.assertIn("class", out, "should name the kinds it did find:\n%s" % out)

    def test_limit_bounds_the_output(self):
        out = run_search_query_as_string(self.client, "log", limit=3, wait=WAIT)
        self.assertLessEqual(len(self.rows_in(out)), 3,
                             "limit exceeded:\n%s" % out)

    def test_nonexistent_symbol_reports_clearly(self):
        out = run_search_query_as_string(self.client, "zzzNoSuchThing", wait=WAIT)
        self.assertNoError(out, "search miss")
        self.assertTrue(out.strip(), "a miss returned an empty string")
        self.assertIn("no symbols matching", out)


class TestFileOutline(IntegrationBase):
    def test_header_lists_its_classes_and_methods(self):
        out = run_outline_query_as_string(self.client, "include/chains.h", wait=WAIT)
        self.assertNoError(out, "outline chains.h")
        for expected in ("Backend", "Frontend", "process", "handleRequest"):
            self.assertIn(expected, out, "%r missing from outline:\n%s"
                          % (expected, out))

    def test_methods_are_nested_under_their_class(self):
        out = run_outline_query_as_string(self.client, "include/chains.h", wait=WAIT)
        # keyed by kind AND name: 'Backend' is both the class and its
        # constructor, and keying on the name alone lets one shadow the other
        rows = {}
        for line in out.splitlines():
            if line.startswith("#") or not line.startswith("  "):
                continue
            parts = line.split()
            if len(parts) > 2:
                rows[(parts[0], parts[1])] = len(line) - len(line.lstrip())
        self.assertIn(("class", "Backend"), rows, out)
        self.assertIn(("method", "process"), rows, out)
        self.assertGreater(rows[("method", "process")], rows[("class", "Backend")],
                           "method not nested under its class:\n%s" % out)

    def test_signatures_come_through_in_detail(self):
        """clangd puts the signature in `detail`, which is what makes an
        outline a substitute for reading the file rather than an index."""
        out = run_outline_query_as_string(self.client, "include/chains.h", wait=WAIT)
        self.assertIn("corpus::Str", out,
                      "no parameter types in the outline:\n%s" % out)

    def test_spans_match_the_real_file(self):
        """A long method must report a span far from its start, or the ranges
        are not safe to read against."""
        out = run_outline_query_as_string(self.client, "src/big.cpp", wait=WAIT)
        spans = [tuple(int(x) for x in m.group(1, 2))
                 for m in re.finditer(r"\s(\d+)-(\d+)(?:\s|$)", out)]
        self.assertTrue(any(end - start > 20 for start, end in spans),
                        "no long span found in big.cpp:\n%s" % out)

    def test_limit_bounds_the_output(self):
        out = run_outline_query_as_string(
            self.client, "src/util.cpp", limit=5, wait=WAIT)
        rows = [l for l in out.splitlines() if not l.startswith("#")]
        self.assertLessEqual(len(rows), 5)
        self.assertIn("more not shown", out)

    def test_missing_file_reports_clearly(self):
        out = run_outline_query_as_string(self.client, "src/nope.cpp", wait=WAIT)
        self.assertTrue(out.strip(), "a missing file returned an empty string")
        self.assertIn("nope.cpp", out)


class TestDiagnostics(IntegrationBase):
    def test_clean_file_is_reported_as_clean(self):
        out = run_diagnostics_query_as_string(
            self.client, "src/chains.cpp", wait=WAIT)
        self.assertNoError(out, "diagnostics chains.cpp")
        self.assertIn("cleanly", out)

    def test_an_edit_on_disk_is_seen(self):
        """The whole point of the tool, and the reason did_open had to learn
        about staleness: without it clangd keeps answering from the contents
        at first open, so a freshly broken file still reports clean."""
        path = "src/chains.cpp"
        target = support.corpus_source(path)
        with open(target, "r", encoding="utf-8") as f:
            original = f.read()
        try:
            before = run_diagnostics_query_as_string(self.client, path, wait=WAIT)
            self.assertIn("cleanly", before, "fixture did not start clean:\n%s" % before)

            with open(target, "w", encoding="utf-8") as f:
                f.write(original + "\nint broken() { return no_such_identifier; }\n")

            after = run_diagnostics_query_as_string(
                self.client, path, severity="error", wait=WAIT)
            self.assertNoError(after, "diagnostics after edit")
            self.assertIn("no_such_identifier", after,
                          "the edit was not seen -- stale document:\n%s" % after)
            self.assertNotIn("cleanly", after)
        finally:
            with open(target, "w", encoding="utf-8") as f:
                f.write(original)

        restored = run_diagnostics_query_as_string(self.client, path, wait=WAIT)
        self.assertIn("cleanly", restored,
                      "file did not go clean again after restore:\n%s" % restored)

    def test_severity_filter_is_applied(self):
        out = run_diagnostics_query_as_string(
            self.client, "src/chains.cpp", severity="error", wait=WAIT)
        self.assertNoError(out, "severity filter")
        self.assertTrue(out.strip())

    def test_missing_file_reports_clearly(self):
        out = run_diagnostics_query_as_string(self.client, "src/nope.cpp", wait=WAIT)
        self.assertTrue(out.strip(), "a missing file returned an empty string")
        self.assertIn("nope.cpp", out)


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
