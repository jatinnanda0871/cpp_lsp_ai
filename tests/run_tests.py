#!/usr/bin/env python3
"""Run the clangq test suite.

    python3 tests/run_tests.py              # everything available
    python3 tests/run_tests.py --fast       # unit tests only (no clangd needed)
    python3 tests/run_tests.py --list       # show what would run, and why

Exit code is 0 only if every test that ran passed. Suites whose dependencies
are missing (clangd, the `mcp` package) are SKIPPED and reported, not failed.
"""
import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  (needs the path set up first)

UNIT_SUITES = ["test_unit"]
MCP_SUITES = ["test_mcp", "test_negative"]
INTEGRATION_SUITES = ["test_integration", "test_mcp_integration"]


def environment_report():
    clangd = support.find_clangd()
    try:
        import clangq_mcp  # noqa: F401
        mcp_ok, mcp_note = True, "installed"
    except Exception as e:
        mcp_ok, mcp_note = False, "NOT installed (%s)" % type(e).__name__
    lines = [
        "clangd:  %s" % (clangd or "NOT FOUND (integration tests will skip)"),
        "         %s" % (support.clangd_version(clangd) if clangd else ""),
        "mcp pkg: %s" % mcp_note,
        "corpus:  %s" % support.CORPUS_DIR,
    ]
    return clangd, mcp_ok, "\n".join(l for l in lines if l.strip())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", action="store_true",
                    help="unit tests only; skip anything needing clangd")
    ap.add_argument("--list", action="store_true", help="show the plan and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    clangd, mcp_ok, report = environment_report()
    print(report)
    print("-" * 70)

    names = list(UNIT_SUITES)
    if mcp_ok:
        names += MCP_SUITES
    if not args.fast and clangd:
        names += INTEGRATION_SUITES
        # The corpus needs a compile_commands.json before clangd can index it.
        try:
            support.ensure_corpus_db()
        except Exception as e:
            print("could not prepare the corpus: %s" % e)
            return 2

    if args.list:
        print("would run: %s" % ", ".join(names))
        return 0

    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(n) for n in names)
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
