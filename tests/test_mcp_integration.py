"""End-to-end tests through the MCP server's own entry point (_run_tool),
against a real clangd indexing fixtures/corpus.

This is the closest thing to what the assistant host actually does, including
the case that originally failed: several tool calls fired at once, each
dispatched to a worker thread sharing one warm clangd.

Skipped when clangd or the `mcp` package is missing.

    python3 -m unittest tests.test_mcp_integration -v
"""
import os
import sys
import threading
import unittest

# So `python3 -m unittest tests.test_mcp_integration` works from the repo root,
# where tests/ is not on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402  (needs the path set up first)

try:
    import clangq_mcp
    MCP_AVAILABLE = True
except Exception:                # pragma: no cover - depends on environment
    MCP_AVAILABLE = False

CLANGD = support.find_clangd()
BAD_OUTPUT = ("Traceback", "Error running query", "NoneType",
              "list index out of range", "AttributeError", "TypeError",
              "Error starting clangd")


def setUpModule():
    if CLANGD is None or not MCP_AVAILABLE:
        return
    support.ensure_corpus_db()
    # Point the server's client factory at the corpus with the right clangd
    # binary; the shipped default assumes a plain `clangd` on PATH.
    from clangd_client import ClangdClient
    ccdir = os.path.join(support.CORPUS_BUILD_DIR)

    def factory(root, request_timeout=None):
        # Ignore the caller's timeout -- this suite wants a generous, fixed
        # one regardless of what clangq_mcp.REQUEST_TIMEOUT_S is set to.
        return ClangdClient(root, compile_commands_dir=ccdir,
                            clangd=CLANGD, request_timeout=60)

    clangq_mcp.ClangdClient = factory
    clangq_mcp._clients.clear()
    clangq_mcp._client_locks.clear()


def tearDownModule():
    if not MCP_AVAILABLE:
        return
    for client in list(clangq_mcp._clients.values()):
        try:
            client.shutdown()
        except Exception:
            pass
    clangq_mcp._clients.clear()


@unittest.skipIf(CLANGD is None, "clangd not installed")
@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class TestMcpToolsEndToEnd(unittest.TestCase):
    ROOT = support.CORPUS_DIR

    def call(self, tool, **args):
        args.setdefault("root", self.ROOT)
        out = clangq_mcp._run_tool(tool, args)
        self.assertIsInstance(out, str)
        for bad in BAD_OUTPUT:
            self.assertNotIn(bad, out, "%s leaked %r:\n%s" % (tool, bad, out))
        self.assertTrue(out.strip(), "%s returned an EMPTY string" % tool)
        return out

    def test_every_tool_answers(self):
        self.call("get_function_info", name="runPipeline")
        self.call("get_class_info", name="Shape")
        self.call("get_macro_info", name="CORPUS_MAX_ITEMS")
        self.call("get_struct_info", name="Point")
        self.call("get_incoming_calls", name="process")
        self.call("get_hover_info",
                  path=support.corpus_source("src/big.cpp"),
                  line=support.line_of("src/big.cpp", "int BigService::runPipeline"),
                  col=len("int BigService::"))

    def test_every_tool_handles_a_missing_symbol(self):
        for tool, key in (("get_function_info", "name"), ("get_class_info", "name"),
                          ("get_macro_info", "name"), ("get_struct_info", "name"),
                          ("get_incoming_calls", "name")):
            out = self.call(tool, **{key: "NoSuchThing_zzz"})
            self.assertTrue(len(out) > 10, "%s gave a uselessly terse answer: %r" % (tool, out))

    def test_reported_paths_are_repo_relative(self):
        """Paths must be anchored on the repo root, not the server's cwd."""
        out = self.call("get_function_info", name="runPipeline")
        self.assertIn("src", out)
        self.assertNotIn(support.CORPUS_DIR, out,
                         "absolute corpus path leaked into output:\n%s" % out)

    def test_paths_do_not_change_with_cwd(self):
        """Regression: output used to be relative to the process cwd, so the
        same file was named differently depending on where the host launched
        the server."""
        import tempfile
        original = os.getcwd()
        outputs = []
        try:
            for d in (support.CORPUS_DIR, support.REPO_ROOT, tempfile.gettempdir()):
                os.chdir(d)
                outputs.append(self.call("get_function_info", name="tinyOne"))
        finally:
            os.chdir(original)
        self.assertEqual(len(set(outputs)), 1,
                         "output varied with cwd:\n%s" % "\n---\n".join(outputs))

    def test_macro_lookup_on_a_cold_client(self):
        """clangd cannot see a macro until the defining file is open. A fresh
        server must still answer instead of timing out and claiming the macro
        does not exist."""
        for client in list(clangq_mcp._clients.values()):
            try:
                client.shutdown()
            except Exception:
                pass
        clangq_mcp._clients.clear()

        out = self.call("get_macro_info", name="CORPUS_LOG_LEVEL")
        self.assertIn("macro decl", out, out)
        self.assertNotIn("no macro found", out)


@unittest.skipIf(CLANGD is None, "clangd not installed")
@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class TestConcurrentToolCalls(unittest.TestCase):
    """The original production failure: the host fires several tool calls at
    once and the server falls over."""

    ROOT = support.CORPUS_DIR

    def test_many_mixed_tools_in_parallel(self):
        calls = [
            ("get_function_info", {"name": "runPipeline"}),
            ("get_function_info", {"name": "isEquilateral"}),
            ("get_class_info", {"name": "Shape"}),
            ("get_class_info", {"name": "Calculator"}),
            ("get_class_info", {"name": "BigService"}),
            ("get_macro_info", {"name": "CORPUS_MAX_ITEMS"}),
            ("get_macro_info", {"name": "CORPUS_CLAMP"}),
            ("get_struct_info", {"name": "Point"}),
            ("get_struct_info", {"name": "GeoCoord"}),
            ("get_incoming_calls", {"name": "process"}),
            ("get_incoming_calls", {"name": "validate"}),
            ("get_function_info", {"name": "tinyOne"}),
        ]
        results, errors = {}, []

        def worker(i, tool, args):
            try:
                a = dict(args)
                a["root"] = self.ROOT
                results[i] = (tool, clangq_mcp._run_tool(tool, a))
            except Exception as e:
                errors.append((tool, repr(e)))

        threads = [threading.Thread(target=worker, args=(i, t, a))
                   for i, (t, a) in enumerate(calls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)

        self.assertEqual(errors, [], "parallel tool calls raised: %s" % errors)
        self.assertEqual(len(results), len(calls), "some calls produced nothing")
        for i, (tool, out) in results.items():
            for bad in BAD_OUTPUT:
                self.assertNotIn(bad, out, "%s leaked %r:\n%s" % (tool, bad, out))
            self.assertTrue(out.strip(), "%s returned EMPTY" % tool)

    def test_parallel_calls_do_not_cross_answers(self):
        """Replies are demultiplexed by request id; a mix-up would return one
        symbol's answer for another symbol's question."""
        names = ["runPipeline", "formatReport", "validateConfig", "quickAdd",
                 "computeChecksum", "tinyOne", "tinyTwo", "tinyThree"]
        results, errors = {}, []

        def worker(name):
            try:
                results[name] = clangq_mcp._run_tool(
                    "get_function_info", {"root": self.ROOT, "name": name})
            except Exception as e:
                errors.append((name, repr(e)))

        threads = [threading.Thread(target=worker, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)

        self.assertEqual(errors, [])
        for name, out in results.items():
            self.assertIn(name, out,
                          "answer for %s does not mention it:\n%s" % (name, out[:400]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
