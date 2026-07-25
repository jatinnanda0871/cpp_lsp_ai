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
clangd_query_engine.py  (CLI: refs / callers / all / class / macro / struct / hover / shell)
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

```bash
# One-shot queries (auto-starts a daemon for the repo, reuses it after)
./clangd_query_engine.py all     processRequest --root /path/to/repo
./clangd_query_engine.py refs    handleRequest  --root /path/to/repo
./clangd_query_engine.py callers doThing        --root /path/to/repo

# Class queries: rolls up every method's refs/callers in one call
./clangd_query_engine.py all Parser --class --root /path/to/repo

# Macro queries: declaration + every expansion site
./clangd_query_engine.py all LOG_DEBUG --macro --root /path/to/repo

# Interactive REPL (clangd stays warm across queries in one process)
./clangd_query_engine.py shell --root /path/to/repo

# Daemon management
./clangd_query_engine.py daemon --root /path/to/repo -v   # run in foreground, watch logs
./clangd_query_engine.py stop   --root /path/to/repo      # shut the daemon down
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--root` | Repo root (default: cwd) |
| `--ccdir` | Directory containing `compile_commands.json`, if not `<root>`/`<root>/build` |
| `--class` | Treat `name` as a class; queries all of its methods |
| `--macro` | Treat `name` as a macro |
| `--include-decl` | Include the declaration itself in `refs` results |
| `--no-daemon` | Run the query in-process instead of via the daemon |
| `--wait` | Seconds to wait for clangd's background index to warm up |
| `-v` / `--verbose` | Print progress/debug info to stderr |

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
           "get_function_info",
           "get_class_info",
           "get_macro_info",
           "get_struct_info",
           "get_hover_info",
           "get_incoming_calls"
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
| `get_function_info` | Definition, declaration, references, and callers for a function (plain name or `Class::method`) |
| `get_class_info` | Declaration location plus definition/refs/callers for every method on a class |
| `get_macro_info` | Macro declaration location and all usages/expansion sites |
| `get_struct_info` | Struct declaration/definition location |
| `get_hover_info` | Type/signature/doc info for a symbol at a specific file position (like IDE hover) |
| `get_incoming_calls` | All functions/methods that call a given function |

## Notes

- The daemon socket/log paths are derived from a hash of the repo root plus
  the current OS user, so multiple repos (and multiple users on a shared
  machine) each get their own isolated daemon.
- Outgoing call hierarchy is not exposed — clangd only supports incoming
  calls (`callers`) reliably; the tool surfaces that limitation explicitly
  rather than silently returning nothing.
