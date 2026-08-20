# cpp_lsp_ai

A semantic code-intelligence tool for large C/C++ codebases, built directly on
[clangd](https://clangd.llvm.org/). It answers questions like *"where is this
function defined?"*, *"who calls this?"*, *"what does this macro expand to and
where is it used?"* — accurately, because the answers come from clangd's own
AST-level index rather than text search.

It ships two ways to use the same engine:

- **A CLI** (`clangd_query_engine.py`) for querying a repo from the terminal or scripts.
- **An MCP server** (`clangq_mcp.py`) that exposes the same queries as tools to
  AI coding assistants (Claude Code, Claude Desktop, Roo Code, etc.), so the
  assistant can navigate a C++ codebase semantically instead of grepping it.

## Why this instead of grep/ripgrep?

Text search can't tell the difference between a function call, a comment
mentioning the same word, an unrelated overload, or a macro expansion. clangd
already builds a full semantic index of the codebase to power IDE features —
this tool just gives you (or your AI assistant) a fast, scriptable way to ask
it questions directly, without needing a separate custom index, database, or
ripgrep-based heuristics:

- **Accurate** — references, callers, and definitions are resolved from the
  compiler's own AST, so overloads, macros, virtual calls, and templates are
  handled correctly.
- **Fast after the first query** — a background daemon keeps clangd warm and
  the index loaded in memory, so only the *first* query on a repo pays the
  indexing cost. Every query after that is near-instant.
- **Zero extra infrastructure** — no code.db to build or maintain, no
  separate indexing pipeline. It reuses `compile_commands.json`, which most
  C++ build systems already produce.
- **Class- and macro-aware** — a single query can pull every method on a
  class (with each method's own refs/callers), or every expansion site of a
  macro, in one shot.
- **AI-assistant ready** — via the MCP server, an assistant can ask
  "who calls `Parser::parse`?" and get a precise answer instead of guessing
  from surrounding text.

## How it works

```
clangd_query_engine.py  (CLI: refs / callers / all / struct / search / outline /
                          diagnostics / hover / incoming / implementations / enum /
                          switch-header / includes / rename; shell / daemon / stop)
        │
        ▼
  daemon (background process, one per repo root)
        │  Unix domain socket (auto-started/reused, keeps clangd warm)
        ▼
  clangd_client.py  (minimal LSP client: JSON-RPC over stdio)
        │
        ▼
      clangd  (reads compile_commands.json, builds/serves the semantic index)
```

The first invocation for a given repo root spawns a daemon that launches
`clangd`, opens one file to trigger indexing, and waits for the background
index to warm up. Every subsequent CLI call (or MCP tool call) for that same
root talks to the already-running daemon over a Unix socket, so there's no
repeated startup/indexing cost. Use `stop` to shut the daemon down, or
`--no-daemon` to force a one-shot in-process run.

## Requirements

- Python 3.8+ (standard library only for the CLI/daemon; no third-party deps)
- `clangd` on `PATH` (or pass `--clangd /path/to/clangd-14`, etc.)
- A `compile_commands.json` for the target repo, auto-discovered in
  `<root>` then `<root>/build` (override with `--ccdir`). Most build systems
  can generate this:
  - CMake: `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ...`
  - Make-based projects: [`bear`](https://github.com/rizsotto/Bear) or
    `intercept-build`
- For the MCP server only: the [`mcp`](https://pypi.org/project/mcp/) Python
  package (`pip install mcp`)
- Unix domain sockets are used for the daemon transport, so the daemon/CLI
  need a Unix-like environment (Linux, macOS, or WSL on Windows).

## CLI usage

Every query the MCP server exposes as a tool (see below) also has a CLI
subcommand, so the CLI and the assistant-facing server are two front ends on
the same engine, not two different feature sets.

```bash
# One-shot queries (auto-starts a daemon for the repo, reuses it after)
./clangd_query_engine.py all     processRequest --root /path/to/repo
./clangd_query_engine.py refs    handleRequest  --root /path/to/repo
./clangd_query_engine.py callers doThing        --root /path/to/repo

# Class queries: rolls up every method's refs/callers in one call
./clangd_query_engine.py all Parser --class --root /path/to/repo

# Macro queries: declaration + every expansion site
./clangd_query_engine.py all LOG_DEBUG --macro --root /path/to/repo

# Struct declaration (including typedef struct)
./clangd_query_engine.py struct Point --root /path/to/repo

# Fuzzy symbol search -- the way in when the exact name isn't known yet
./clangd_query_engine.py search Backend --kind class --root /path/to/repo

# Every symbol in one file, nested, with line spans and signatures
./clangd_query_engine.py outline src/parser.cpp --root /path/to/repo

# Compiler diagnostics for one file, reflecting what's on disk right now
./clangd_query_engine.py diagnostics src/parser.cpp --severity error --root /path/to/repo

# Type/signature/doc for the symbol at a position (0-based line/col)
./clangd_query_engine.py hover src/parser.cpp 42 8 --root /path/to/repo

# Callers only, without the def/decl header lines that `callers` prints
./clangd_query_engine.py incoming doThing --root /path/to/repo

# Overriding definitions of a virtual method/function
./clangd_query_engine.py implementations area --root /path/to/repo

# Enum declaration and members
./clangd_query_engine.py enum LogLevel --root /path/to/repo

# The counterpart header/source file for a path
./clangd_query_engine.py switch-header src/parser.cpp --root /path/to/repo

# What a file includes, and what includes it (text scan, not the index)
./clangd_query_engine.py includes include/parser.h --root /path/to/repo

# PREVIEW ONLY: every edit site a workspace rename would touch -- writes nothing
./clangd_query_engine.py rename oldName newName --root /path/to/repo

# Interactive REPL (clangd stays warm across queries in one process; all of
# the above are available as REPL commands too -- type 'help')
./clangd_query_engine.py shell --root /path/to/repo

# Daemon management
./clangd_query_engine.py daemon --root /path/to/repo -v   # run in foreground, watch logs
./clangd_query_engine.py stop   --root /path/to/repo      # shut the daemon down
```

Useful flags (present on every subcommand):

| Flag | Purpose |
|---|---|
| `--root` | Repo root (default: cwd) |
| `--ccdir` | Directory containing `compile_commands.json`, if not `<root>`/`<root>/build` |
| `--no-daemon` | Run the query in-process instead of via the daemon |
| `--wait` | Seconds to wait for clangd's background index / a single request |
| `--req-timeout` | Per-request timeout to clangd (raise for heavy translation units) |
| `--clangd` | clangd binary name/path (e.g. `clangd-14`) |
| `-v` / `--verbose` | Print progress/debug info to stderr |

Flags specific to `refs` / `callers` / `all`:

| Flag | Purpose |
|---|---|
| `--class` | Treat `name` as a class; queries all of its methods |
| `--macro` | Treat `name` as a macro |
| `--include-decl` | Include the declaration itself in `refs` results |

Flags specific to `search` / `outline` / `diagnostics`:

| Flag | Purpose |
|---|---|
| `--kind` | (`search`) Restrict to one symbol kind: function, class, struct, macro, ... |
| `--limit` | (`search`, `outline`) Max rows to return |
| `--severity` | (`diagnostics`) Lowest severity to report: `error`, `warning`, `all` |

## Using it as an MCP server (AI assistant integration)

`clangq_mcp.py` exposes the same query engine as MCP tools, so any
MCP-compatible assistant can call them directly instead of relying on text
search over the codebase.

1. Install the MCP package: `pip install mcp`
2. Point your assistant's MCP config at the server, e.g. in `mcp.json`:

   ```json
   {
     "mcpServers": {
       "clangq": {
         "command": "/usr/bin/python3",
         "args": ["<workspace_folder>/scripts/clangq_mcp.py"],
         "env": {
           "PYTHONPATH": "<absolute_path_to_python_packages>"
         },
         "disabled": false,
         "alwaysAllow": [
           "search_symbols",
           "get_function_info",
           "get_class_info",
           "get_macro_info",
           "get_struct_info",
           "get_file_outline",
           "get_diagnostics",
           "get_hover_info",
           "get_incoming_calls",
           "get_implementations",
           "get_enum_info",
           "switch_source_header",
           "get_includes",
           "preview_rename"
         ]
       }
     }
   }
   ```

3. Restart/reload the assistant so it picks up the new MCP server.

Each tool call takes a `root` (repo path) parameter, so a single server
instance can serve queries across multiple repos — it keeps one warm
`ClangdClient` per root.

### Available MCP tools

| Tool | Description |
|---|---|
| `search_symbols` | Fuzzy symbol search by name or fragment — the entry point when the exact name isn't known yet. Returns qualified names the tools below accept |
| `get_function_info` | Definition, declaration, references, and callers for a function (plain name or `Class::method`) |
| `get_class_info` | Declaration location plus definition/refs/callers for every method on a class |
| `get_macro_info` | Macro declaration location and all usages/expansion sites |
| `get_struct_info` | Struct declaration/definition location |
| `get_file_outline` | Every symbol declared in one file — nested, with line spans and signatures |
| `get_diagnostics` | clangd's compiler errors/warnings for one file, reflecting what is on disk now |
| `get_hover_info` | Type/signature/doc info for a symbol at a specific file position (like IDE hover) |
| `get_incoming_calls` | All functions/methods that call a given function |
| `get_implementations` | Overriding definitions of a virtual method/function — the complement of `get_incoming_calls` |
| `get_enum_info` | Enum declaration location and its members |
| `switch_source_header` | The counterpart header/source file for a path |
| `get_includes` | What a file includes, and what includes it (best-effort text scan, not the index) |
| `preview_rename` | **Preview only** — every edit site a workspace-wide rename would touch; never writes to disk |

#### `search_symbols` — finding a name to query

Every other tool needs an exact symbol name. `search_symbols` is how an
assistant gets one without falling back to grep: it fuzzy-matches a name or
fragment against clangd's index and returns **qualified** names
(`chain::Backend::process`) that the other tools resolve directly.

| Parameter | Purpose |
|---|---|
| `query` | Name or fragment, e.g. `parse`, `Handler` (required) |
| `kind` | Narrow to one kind: `function`, `method`, `class`, `struct`, `enum`, `variable`, `field`, `namespace`, `macro`, `type` |
| `limit` | Max rows, default 30, cap 200 |

```
# 6 symbols matching 'Backend' (any kind), showing 6
# exact (2):
  class       chain::Backend                  include/chains.h:19
  constructor chain::Backend::Backend         src/chains.cpp:6
# fuzzy -- nothing else is named 'Backend' exactly (4):
  method      chain::Frontend::setBackend     src/chains.cpp:61
  field       chain::Frontend::m_backend      include/chains.h:51
```

Results are split into exact and fuzzy sections, and exact hits are never the
ones dropped by `limit`. Two behaviours worth knowing:

- **Kinds are what clangd reports, not what the LSP spec says.** clangd files
  C++ structs (including `typedef struct`) under `class`, so the `struct`
  filter deliberately matches both — a spec-literal filter finds zero structs
  in a repo full of them.
- **Macros are found via a fallback.** `workspace/symbol` never reports a
  `#define` until its defining file is open, so a miss triggers the same
  locate-and-open path `get_macro_info` uses. Without it, every macro search
  would come back as "check the spelling".

A filter that matches nothing says so distinctly from a name that matches
nothing (`6 symbols match 'Backend', but none of kind 'macro' (found: class,
constructor, field, method)`), so a too-narrow filter is never mistaken for an
absent symbol.

#### `get_file_outline` — reading a file without reading it

Takes a `path` (repo-relative or absolute) and an optional `limit` (default
300). clangd puts each symbol's signature in `detail`, so the outline carries
types without a second query, and the spans give exact ranges to read next:

```
# Outline of include/chains.h (19 symbols)
  namespace   chain                    17-54
    class       Backend                  19-35
      method      process                  24-24   int (const corpus::Str &)
      method      processedCount           30-30   int () const
      field       m_count                  34-34   int
```

#### `get_diagnostics` — checking an edit without building

Takes a `path` and an optional `severity` (`error`, `warning` = errors and
warnings, or `all`, the default). This is the edit loop: change a file, ask
what broke, fix it — no build system involved.

```
# Diagnostics for src/chains.cpp: 1 error
  error    src/chains.cpp:71:27  Use of undeclared identifier 'foo' [clang:undeclared_var_use]
```

Two distinctions it is careful about:

- **"Not reported yet" is never rendered as "clean."** clangd pushes
  diagnostics unprompted when it finishes parsing, so nothing arriving within
  the timeout means only that — it says so explicitly rather than issuing a
  false all-clear on a file that may not compile.
- **A file outside `compile_commands.json`** is parsed with guessed flags and
  can report spurious errors. The tool description says so, and a repo with no
  compilation database at all already gets a warning on every call.

### Files are re-read when they change

`did_open` now stats a file on each use and pushes a `didChange` when it has
changed on disk. Without this a long-lived client answers from the contents at
first open — silently, and for the life of the process — which would make
`get_diagnostics` useless (it would report a freshly broken file as clean) and
`get_hover_info` quietly wrong after any edit. Unchanged files cost one `stat`,
not a re-read.

## Notes

- The daemon socket/log paths are derived from a hash of the repo root plus
  the current OS user, so multiple repos (and multiple users on a shared
  machine) each get their own isolated daemon.
- Outgoing call hierarchy is not exposed — clangd only supports incoming
  calls (`callers`) reliably; the tool surfaces that limitation explicitly
  rather than silently returning nothing.
- `rename` returns a preview of every edit site clangd computed; it never
  writes to disk — apply the listed edits yourself. It also refuses rather
  than guesses when the name is ambiguous (an overload set) or a macro,
  since acting on the wrong symbol is a materially worse failure than a
  read query guessing wrong.
- `includes` is a best-effort text scan of `#include` lines, not a query
  against clangd's index — it does not evaluate `#ifdef`, and two files
  sharing a basename in different directories are indistinguishable to it.
