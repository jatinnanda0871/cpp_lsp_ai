#!/usr/bin/python3
"""MCP server exposing the clangq semantic queries as assistant tools.

Tools are declared once in TOOLS. A tool's Param list drives both the JSON
schema sent to the host and the validation of incoming calls, so the two
cannot drift apart.

    call_tool -> asyncio.to_thread -> _run_tool -> handler -> query engine
"""
import asyncio
import os
import sys
import threading

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

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
    SEARCH_KIND_FILTERS,
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    DEFAULT_OUTLINE_LIMIT,
    MAX_OUTLINE_LIMIT,
    SEVERITY_FILTERS,
    DEFAULT_SEVERITY,
)

# ============================ tunables ============================
# Per-request timeout given to ClangdClient -- bounds any SINGLE LSP round
# trip (documentSymbol, references, etc.). Distinct from QUERY_WAIT_S below,
# which bounds retrying workspace/symbol while the index warms. The CLI has
# --req-timeout for this ("raise for heavy TUs"); MCP takes no CLI flags, so
# an env var is the only way a host can raise it for a large repo.
REQUEST_TIMEOUT_S = int(os.environ.get("CLANGQ_REQ_TIMEOUT", "30"))

# Budget for a cold index on a large repo. ClangdClient cuts this short once
# the index is serving, so a typo doesn't cost the full 120s.
QUERY_WAIT_S = 120

# Hover and the outline read the open file's AST -- no index needed, so they
# don't need QUERY_WAIT_S's cold-index budget, just enough for one parse.
HOVER_WAIT_S = 10

# Diagnostics arrive unprompted once clangd finishes parsing the TU, so this
# budgets a full reparse of a heavy translation unit -- longer than hover,
# which only queries an AST that already exists.
DIAGNOSTICS_WAIT_S = 30

VALID_MODES = ("refs", "callers", "all")

server = Server("clangq")


# ======================= warm clangd per repo root =======================
# Indexing is the expensive part, so one client stays alive per root.
_clients = {}
_client_locks = {}
_clients_lock = threading.Lock()


def _lock_for(root):
    """Startup lock for one root. Per-root, so a cold start for one repo
    doesn't block queries against an already-warm one."""
    with _clients_lock:
        if root not in _client_locks:
            _client_locks[root] = threading.Lock()
        return _client_locks[root]


def get_client(root):
    """The warm client for `root`, started or replaced as needed.

    Locked because tool calls run in worker threads: two concurrent
    first-time calls would otherwise start two clangd processes.
    """
    with _lock_for(root):
        client = _clients.get(root)

        # A dead clangd would fail every later request, so retire it.
        if client is not None and not client.is_alive():
            try:
                client.shutdown()
            except Exception:
                pass
            del _clients[root]
            client = None

        if client is None:
            client = ClangdClient(root, request_timeout=REQUEST_TIMEOUT_S).start()
            client.prime_index()
            _clients[root] = client
        return client


def shutdown_all_clients():
    """Stop every warm clangd (server exit, and the tests)."""
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        try:
            client.shutdown()
        except Exception:
            pass


# ============================= parameters =============================
# A Param carries its own schema and its own validator, so the advertised
# contract and the enforced one are one declaration. Validators return an
# error string, or None if the value is fine.
_MISSING = object()


def _type_name(value):
    """JSON type name -- the caller speaks JSON, not Python."""
    return "null" if value is None else type(value).__name__


class Param:
    def __init__(self, name, schema, required=True, default=None,
                 validate=None, hint=""):
        self.name = name
        self.schema = schema
        self.required = required
        self.default = default
        self.hint = hint
        self._validate = validate

    def missing_error(self):
        return "Error: %s is required%s" % (
            self.name, " (%s)" % self.hint if self.hint else "")

    def resolve(self, arguments):
        """Return (error, value) for this parameter.

        Blank counts as missing only when required ("root is required" beats
        a type complaint). For an optional param a blank is a caller mistake,
        not an omission, so it must not silently become the default.
        """
        raw = arguments.get(self.name, _MISSING)
        supplied = raw is not _MISSING and raw is not None
        if supplied and self.required and isinstance(raw, str) and not raw.strip():
            supplied = False

        if not supplied:
            return (self.missing_error(), None) if self.required else (None, self.default)

        error = self._validate(self.name, raw) if self._validate else None
        return error, (None if error else raw)


def _validate_repo_root(label, value):
    """Reported paths are relativised against the root, so a file or a
    missing directory yields nonsense like '(def .:171-171)'."""
    if not isinstance(value, str):
        return "Error: %s must be a string, got %s (%r)" % (
            label, _type_name(value), value)
    if not os.path.exists(value):
        return "Error: %s path does not exist: %r" % (label, value)
    if not os.path.isdir(value):
        return "Error: %s path is not a directory: %r" % (label, value)
    return None


def _validate_symbol_name(label, value):
    """Checked before clangd sees it: a non-string comes back as a bare
    JSON-RPC -32602, naming neither the argument nor the problem."""
    if not isinstance(value, str):
        return "Error: %s must be a string, got %s (%r)" % (
            label, _type_name(value), value)
    return None


def _validate_mode(label, value):
    """An unknown mode matches no output section, so the caller would get a
    definition line and silently none of the refs/callers they asked for."""
    if not isinstance(value, str) or value not in VALID_MODES:
        return "Error: %s must be one of %s, got %r" % (
            label, ", ".join(VALID_MODES), value)
    return None


def _validate_file_path(label, value):
    if not isinstance(value, str):
        return "Error: %s must be a string, got %s (%r)" % (
            label, _type_name(value), value)
    return None


def _validate_position(label, value):
    """bool subclasses int, so `true` would otherwise pass as line 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "Error: %s must be an integer, got %s (%r)" % (
            label, _type_name(value), value)
    if value < 0:
        return "Error: %s must not be negative, got %d" % (label, value)
    return None


def _validate_kind(label, value):
    """Rejected rather than ignored: an unknown kind falling through to an
    unfiltered search returns results that quietly disregard the constraint
    the caller set, which reads as "these are all functions" when they aren't.
    """
    if not isinstance(value, str) or value not in SEARCH_KIND_FILTERS:
        return "Error: %s must be one of %s, got %r" % (
            label, ", ".join(sorted(SEARCH_KIND_FILTERS)), value)
    return None


def _validate_severity(label, value):
    if not isinstance(value, str) or value not in SEVERITY_FILTERS:
        return "Error: %s must be one of %s, got %r" % (
            label, ", ".join(sorted(SEVERITY_FILTERS)), value)
    return None


def _validate_outline_limit(label, value):
    if isinstance(value, bool) or not isinstance(value, int):
        return "Error: %s must be an integer, got %s (%r)" % (
            label, _type_name(value), value)
    if value < 1:
        return "Error: %s must be at least 1, got %d" % (label, value)
    if value > MAX_OUTLINE_LIMIT:
        return "Error: %s must not exceed %d, got %d" % (
            label, MAX_OUTLINE_LIMIT, value)
    return None


def _validate_limit(label, value):
    """bool subclasses int here too, and a cap keeps one call from flooding
    the caller's context with a whole repo's symbol table."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "Error: %s must be an integer, got %s (%r)" % (
            label, _type_name(value), value)
    if value < 1:
        return "Error: %s must be at least 1, got %d" % (label, value)
    if value > MAX_SEARCH_LIMIT:
        return "Error: %s must not exceed %d, got %d" % (
            label, MAX_SEARCH_LIMIT, value)
    return None


ROOT = Param(
    "root",
    {"type": "string", "description": "Repo root path"},
    validate=_validate_repo_root, hint="the repo root path",
)
SYMBOL = Param(
    "name",
    {"type": "string", "description": "Symbol name, plain or qualified"},
    validate=_validate_symbol_name, hint="a non-empty symbol name",
)
MODE = Param(
    "mode",
    {"type": "string", "enum": list(VALID_MODES), "default": "all",
     "description": "Which sections to return"},
    required=False, default="all", validate=_validate_mode,
)
PATH = Param(
    "path",
    {"type": "string", "description": "File path e.g. src/handler.cpp"},
    validate=_validate_file_path, hint="a file path",
)
LINE = Param(
    "line",
    {"type": "integer", "description": "Line number, 0-based"},
    validate=_validate_position, hint="a 0-based integer line number",
)
COL = Param(
    "col",
    {"type": "integer", "description": "Column number, 0-based"},
    validate=_validate_position, hint="a 0-based integer column number",
)
SEARCH_QUERY = Param(
    "query",
    {"type": "string",
     "description": "Name or fragment to search for e.g. 'parse', 'Handler'"},
    validate=_validate_symbol_name, hint="a non-empty search string",
)
SEARCH_KIND = Param(
    "kind",
    {"type": "string", "enum": sorted(SEARCH_KIND_FILTERS),
     "description": "Restrict results to one kind of symbol"},
    required=False, default=None, validate=_validate_kind,
)
SEARCH_LIMIT = Param(
    "limit",
    {"type": "integer", "default": DEFAULT_SEARCH_LIMIT,
     "minimum": 1, "maximum": MAX_SEARCH_LIMIT,
     "description": "Maximum rows to return (default %d)" % DEFAULT_SEARCH_LIMIT},
    required=False, default=DEFAULT_SEARCH_LIMIT, validate=_validate_limit,
)
OUTLINE_LIMIT = Param(
    "limit",
    {"type": "integer", "default": DEFAULT_OUTLINE_LIMIT,
     "minimum": 1, "maximum": MAX_OUTLINE_LIMIT,
     "description": "Maximum rows to return (default %d)" % DEFAULT_OUTLINE_LIMIT},
    required=False, default=DEFAULT_OUTLINE_LIMIT,
    validate=_validate_outline_limit,
)
SEVERITY = Param(
    "severity",
    {"type": "string", "enum": sorted(SEVERITY_FILTERS),
     "default": DEFAULT_SEVERITY,
     "description": "Lowest severity to report: 'error', 'warning' "
                    "(errors+warnings), or 'all'"},
    required=False, default=DEFAULT_SEVERITY, validate=_validate_severity,
)


# ============================== the tools ==============================
class ToolSpec:
    def __init__(self, name, description, params, handler):
        self.name = name
        self.description = description
        self.params = params
        self.handler = handler

    def as_mcp_tool(self):
        return types.Tool(
            name=self.name,
            description=self.description,
            inputSchema={
                "type": "object",
                "properties": {p.name: p.schema for p in self.params},
                "required": [p.name for p in self.params if p.required],
            },
        )

    def resolve_arguments(self, arguments):
        """Validate every declared param. Returns (error, values)."""
        values = {}
        for param in self.params:
            error, value = param.resolve(arguments)
            if error:
                return error, None
            values[param.name] = value
        return None, values


TOOLS = {}


def _tool(name, description, params):
    """Register a tool. The decorated function is its handler, called with
    (client, args) already validated and defaulted."""
    def register(handler):
        TOOLS[name] = ToolSpec(name, description, params, handler)
        return handler
    return register


@_tool(
    "search_symbols",
    "Fuzzy-search the codebase for symbols by name or fragment, e.g. 'parse' "
    "or 'Handler'. Use this FIRST whenever the exact symbol name is not "
    "already known -- it returns qualified names ('Class::method') that every "
    "other tool here accepts, so it is the way in to them. Results are split "
    "into exact and fuzzy matches and can be narrowed with `kind` "
    "(function, class, struct, macro, variable, namespace, ...).",
    [ROOT, SEARCH_QUERY, SEARCH_KIND, SEARCH_LIMIT],
)
def _search_symbols(client, args):
    # QUERY_WAIT_S, not a shorter budget: on a cold index a hasty "nothing
    # found" is indistinguishable to the caller from "no such symbol", and
    # resolve_symbol already fast-fails once the index is known warm.
    return run_search_query_as_string(
        client, args["query"], kind=args["kind"], limit=args["limit"],
        wait=QUERY_WAIT_S)


@_tool(
    "get_function_info",
    "Get definition location, declaration location, references and callers "
    "of a C++ function. Accepts a plain function name e.g. 'PrepareErrMsg' "
    "or a qualified name e.g. 'Parser::PrepareErrMsg'.",
    [ROOT, SYMBOL, MODE],
)
def _get_function_info(client, args):
    return run_query_as_string(client, args["mode"], args["name"], QUERY_WAIT_S)


@_tool(
    "get_class_info",
    "Get declaration location and all methods of a C++ class. For each method "
    "returns definition, declaration, references and callers. Use when asked "
    "about a class structure or workflow.",
    [ROOT, SYMBOL, MODE],
)
def _get_class_info(client, args):
    return run_class_query_as_string(client, args["mode"], args["name"], QUERY_WAIT_S)


@_tool(
    "get_macro_info",
    "Get the declaration location of a C/C++ macro (where it is #defined). "
    "Note clangd does not index macro expansion sites, so usage lists are "
    "incomplete; the answer says so explicitly.",
    [ROOT, SYMBOL, MODE],
)
def _get_macro_info(client, args):
    return run_macro_query_as_string(client, args["mode"], args["name"], QUERY_WAIT_S)


@_tool(
    "get_struct_info",
    "Get the declaration location of a C/C++ struct, including a typedef "
    "struct.",
    [ROOT, SYMBOL],
)
def _get_struct_info(client, args):
    return run_struct_query_as_string(client, args["name"], QUERY_WAIT_S)


@_tool(
    "get_file_outline",
    "List every symbol declared in one file -- classes, methods, functions, "
    "fields -- nested, with line spans and signatures. Use it to orient in an "
    "unfamiliar or large file for a fraction of the cost of reading it, then "
    "read the exact line range you need.",
    [ROOT, PATH, OUTLINE_LIMIT],
)
def _get_file_outline(client, args):
    return run_outline_query_as_string(
        client, args["path"], limit=args["limit"], wait=HOVER_WAIT_S)


@_tool(
    "get_diagnostics",
    "Get clangd's compiler diagnostics (errors, warnings) for one file, "
    "reflecting what is on disk right now. Use it after editing C/C++ to "
    "check the file still parses without running the build. Note a file "
    "outside compile_commands.json is parsed with guessed flags and can "
    "report spurious errors.",
    [ROOT, PATH, SEVERITY],
)
def _get_diagnostics(client, args):
    return run_diagnostics_query_as_string(
        client, args["path"], severity=args["severity"], wait=DIAGNOSTICS_WAIT_S)


@_tool(
    "get_hover_info",
    "Get hover information (type, signature, documentation) for the symbol at "
    "a specific position in a file -- the same info an IDE shows on hover.",
    [ROOT, PATH, LINE, COL],
)
def _get_hover_info(client, args):
    return run_hover_query_as_string(
        client, args["path"], args["line"], args["col"], wait=HOVER_WAIT_S)


@_tool(
    "get_incoming_calls",
    "Get the incoming call hierarchy (callers) for a C++ function: every "
    "function or method that calls it.",
    [ROOT, SYMBOL],
)
def _get_incoming_calls(client, args):
    return run_incoming_calls_query_as_string(client, args["name"], QUERY_WAIT_S)


# ============================== dispatch ==============================
_NO_INDEX_WARNING = (
    "# Warning: no compile_commands.json found under %r (checked that path "
    "and its build/ subdir) -- clangd has no index, so results below are "
    "likely empty regardless of the query.\n\n"
)


def _run_tool(name, arguments):
    """Validate a tool call, run it, return its text.

    Every failure returns a string rather than raising: an exception here
    escapes through asyncio.to_thread and can kill the session, which a
    malformed request must not be able to do. All validation happens up
    front, so handlers see present, typed, defaulted arguments.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return "Unknown tool: %s (known tools: %s)" % (
            name, ", ".join(sorted(TOOLS)))

    # A hand-rolled client can send anything; `.get` on a non-dict raises
    # AttributeError straight out of the worker thread.
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return "Error: arguments must be an object, got %s (%r)" % (
            _type_name(arguments), arguments)

    error, args = spec.resolve_arguments(arguments)
    if error:
        return error

    try:
        client = get_client(args["root"])
    except Exception as e:
        return "Error starting clangd: %s" % e

    # clangd starts fine without a compile DB, it just indexes nothing --
    # and the caller can't tell "no matches" from "no index".
    warning = "" if client._db_found else _NO_INDEX_WARNING % args["root"]

    try:
        return warning + spec.handler(client, args)
    except Exception as e:
        return warning + "Error running query: %s" % e


# ============================ server plumbing ============================
@server.list_tools()
async def list_tools():
    return [spec.as_mcp_tool() for spec in TOOLS.values()]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    # Off the event loop, so other calls dispatch while this waits on clangd.
    result = await asyncio.to_thread(_run_tool, name, arguments)
    return [types.TextContent(type="text", text=result)]

async def main():
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    except Exception as e:
        print("clangq_mcp main() crashed: %s" % e, file=sys.stderr)
    except BaseException as e:
        print("clangq_mcp main() crashed: %s" % e, file=sys.stderr)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("clangq_mcp interrupted", file=sys.stderr)
    except Exception as e:
        print("clangq_mcp asyncio.run() crashed: %s" % e, file=sys.stderr)
        sys.exit(1)
