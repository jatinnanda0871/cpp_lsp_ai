# Test suite

Tests for the clangq query engine, LSP client, and MCP server.

The suite is layered so that most of it runs anywhere, and the parts needing a
language server degrade to *skipped* rather than failed:

| Suite | File | Needs |
|---|---|---|
| Unit | `test_unit.py` | nothing — pure Python |
| MCP server | `test_mcp.py` | the `mcp` package |
| Negative / hostile input | `test_negative.py` | `mcp` (some cases also use `clangd`) |
| Engine integration | `test_integration.py` | a `clangd` binary |
| MCP end-to-end | `test_mcp_integration.py` | `clangd` **and** `mcp` |

## Requirements

**Required**

- Python 3.8 or newer. Only the standard library is used (`unittest`), so
  there is nothing to `pip install` for the unit tests.

**Optional — each unlocks more of the suite**

- **`clangd`** — enables the integration suites. The production target is
  **clangd-14**, and the runner prefers it: it looks for
  `clangd-14`, `clangd-14.0`, `clangd14`, then `clangd-15`…`clangd-18`, then
  plain `clangd`, taking the first one on `PATH`. Override with:

  ```bash
  export CLANGQ_TEST_CLANGD=/usr/lib/llvm-14/bin/clangd
  ```

  Install on Debian/Ubuntu with `sudo apt install clangd-14`.

- **`mcp`** — enables the MCP server suites: `pip install mcp`.

- **`cmake`** — used, when present, to generate the fixture's
  `compile_commands.json`. **Not required**: if cmake is missing the suite
  writes the compilation database directly in Python. clangd only needs the
  compile *flags*, never a compiled binary, so both paths index identically.

Note there is **no C++ compiler or standard library requirement**. The fixture
corpus is deliberately freestanding — it includes no system headers at all (see
[Fixture corpus](#fixture-corpus)).

## Setup

None. Nothing is generated ahead of time, and no environment variables are
needed. The first integration run creates
`tests/fixtures/corpus/build/compile_commands.json` automatically and reuses it
afterwards. That `build/` directory is disposable — delete it to force
regeneration.

## Running

```bash
# from the repo root
python3 tests/run_tests.py            # everything the machine supports
python3 tests/run_tests.py --fast     # unit tests only, no clangd needed
python3 tests/run_tests.py --list     # print the plan and the detected tools
python3 tests/run_tests.py -v         # per-test output
```

The runner prints which `clangd` it found (and its version) before starting, so
a surprising skip is easy to diagnose.

Individual suites work through `unittest` as usual:

```bash
cd tests
python3 -m unittest test_unit -v
python3 -m unittest test_integration.TestIncomingCalls -v
python3 -m unittest test_mcp_integration.TestConcurrentToolCalls.test_many_mixed_tools_in_parallel
```

Two environment variables tune integration runs:

| Variable | Purpose |
|---|---|
| `CLANGQ_TEST_CLANGD` | Use a specific clangd binary |
| `CLANGQ_TEST_WAIT` | Seconds allowed for the index to warm (default `90`) |

Expect the unit suites to finish in seconds and the integration suites to take
a few minutes — clangd has to index the corpus, and several tests deliberately
wait out a lookup deadline.

## Fixture corpus

`fixtures/corpus/` is a small C++ project — **24 classes/structs and 99
function definitions** — built to exercise the shapes that break naive symbol
lookup:

| File | What it covers |
|---|---|
| `shapes.h/.cpp` | pure virtual, virtual with an override, 3-level inheritance; every method declared in the header and defined in the `.cpp` |
| `inline_only.h` | header-only classes where declaration and definition coincide |
| `overloads.h/.cpp` | a 4-way overload set, `const`/non-`const` pair, `static` methods, operator overloads |
| `containers.h/.cpp` | class and function templates, and a **nested** class (`Registry::Entry`) |
| `namespaced.h/.cpp` | nested namespaces, a pure interface, multiple inheritance |
| `util.h/.cpp` | object-like and function-like macros, a plain struct, a `typedef struct`, and deliberately unused/undefined symbols |
| `chains.h/.cpp` | call chains with **exact known caller counts** |
| `big.h/.cpp` | long method bodies (~40 lines) next to one-line ones, to check reported line spans |
| `main.cpp` | ties it together so references and call sites actually exist |
| `prelude.h` | minimal freestanding `Str`/`Vec<T>` replacing `std::string`/`std::vector` |

### Why it uses no standard library

The corpus includes **zero system headers**. This is deliberate, and it was
found the hard way: on a machine without a C++ toolchain, `#include <string>`
fails, clangd's AST is broken, and every query quietly returns *zero*
references and *zero* callers — which looks exactly like a bug in the query
engine. Depending on the host toolchain would make the suite report different
results on different machines, and mask real regressions.

`prelude.h` supplies the few types the corpus needs, so clangd parses every
file cleanly with no compiler installed and the suite is reproducible
everywhere.

If you edit the corpus, keep it freestanding and **C++14-compatible** (clang-14
is the production target). Verify with:

```bash
python3 -m unittest test_integration -v
```

## What the tests assert

Integration tests check **ground truth**, not just absence of exceptions — a
query returning a confidently wrong answer is worse than one that errors. For
example: `Backend::process` must report exactly its three callers;
`Backend::orphan` must report zero without erroring; a long body must report a
span far from its start; a symbol that does not exist must produce an
explanatory message rather than an empty string.

Unit tests are regression guards. Each corresponds to a failure that actually
happened — malformed or explicitly-null LSP payloads, empty results reported as
nothing at all, transport frames that killed the reader thread, and paths that
changed depending on the process working directory.

### Negative tests

`test_negative.py` sends what a real caller can actually get wrong: mistyped
symbols, wrong argument types, a root that does not exist, a malformed request
object, and hostile strings (regex metacharacters, embedded nulls and newlines,
5 000-character names, non-ASCII, shell metacharacters, path traversal). The
contract is deliberately narrow but strict:

1. **never raise** out of `_run_tool` — an exception escaping the worker thread
   propagates into the MCP session instead of being reported to the caller;
2. **never return an empty string** — `""` tells the assistant nothing;
3. **never answer confidently from an approximate match** — `workspace/symbol`
   is a *fuzzy* search and will answer even for `"::"`, so an inexact hit is
   labelled and reported under the name that was actually found;
4. **never misattribute a miss** — a typo must not be blamed on a cold index,
   nor a cold index on a typo.

### Known limitation: macro usage sites

clangd does not index macro *expansion* sites, so `get_macro_info` can only
list the `#define` itself however many times the macro is used. The tool now
says so explicitly, because a bare "1 reference" reads as "this macro is
almost unused", which is worse than saying nothing. `test_negative.py` asserts
that the disclosure is present — if a future clangd starts reporting real
expansion sites, that test is the place to revisit.

## Adding a test

- Put anything that can run without clangd in `test_unit.py`; it is the suite
  that always runs.
- For integration tests, locate positions with `support.line_of(...)` rather
  than hardcoding line numbers, so editing the corpus does not silently break
  assertions.
- Prefer asserting a specific known fact over "did not crash".
