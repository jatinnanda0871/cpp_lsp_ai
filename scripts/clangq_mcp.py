import asyncio
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
)

server = Server("clangq")

# keep warm clients per root -- avoids re-indexing on every call
_clients = {}
_client_locks = {}
_clients_lock = threading.Lock()

def _lock_for(root):
    with _clients_lock:
        if root not in _client_locks:
            _client_locks[root] = threading.Lock()
        return _client_locks[root]

def get_client(root):
    # Query calls run in worker threads (see call_tool below), so two
    # concurrent first-time calls for the same root could otherwise race
    # and start two daemons for it. The lock is per-root rather than
    # global so a slow cold start for one repo doesn't block queries
    # against an already-warm different repo.
    with _lock_for(root):
        client = _clients.get(root)
        # A warm client whose clangd has since died would fail every
        # request forever, so drop it and start a fresh one.
        if client is not None and not client.is_alive():
            try:
                client.shutdown()
            except Exception:
                pass
            del _clients[root]
            client = None
        if client is None:
            client = ClangdClient(root).start()
            client.prime_index()
            _clients[root] = client
        return client

@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="get_function_info",
            description=(
                "Get definition location, declaration location, references "
                "and callers of a C++ function. "
                "Accepts plain function name e.g. 'PrepareErrMsg' "
                "or qualified name e.g. 'Parser::PrepareErrMsg'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Function name or ClassName::methodName"
                    },
                    "root": {
                        "type": "string",
                        "description": "Repo root path"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["refs", "callers", "all"],
                        "default": "all"
                    }
                },
                "required": ["name", "root"]
            }
        ),
        types.Tool(
            name="get_class_info",
            description=(
                "Get declaration location and all methods of a C++ class. "
                "For each method returns definition, declaration, "
                "references and callers. "
                "Use when asked about a class structure or workflow."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Class name e.g. Parser"
                    },
                    "root": {
                        "type": "string",
                        "description": "Repo root path"
                    }
                },
                "required": ["name", "root"]
            }
        ),
        types.Tool(
            name="get_macro_info",
            description=(
                "Get declaration location and all usages/references of a C/C++ macro. "
                "Returns the macro definition location (where it's #defined) "
                "and all files/locations where the macro is used/expansion."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Macro name e.g. LOG_DEBUG"
                    },
                    "root": {
                        "type": "string",
                        "description": "Repo root path"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["refs", "callers", "all"],
                        "default": "all"
                    }
                },
                "required": ["name", "root"]
            }
        ),
        types.Tool(
            name="get_struct_info",
            description=(
                "Get declaration location and all usages/references of a C/C++ struct. "
                "Returns the struct definition location (where it's defined) "
                "and all files/locations where the struct type is used."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Struct name e.g. nvme_ctrl_info"
                    },
                    "root": {
                        "type": "string",
                        "description": "Repo root path"
                    },
                    "include_decl": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include the struct definition in the references list"
                    }
                },
                "required": ["name", "root"]
            }
        ),
        types.Tool(
            name="get_hover_info",
            description=(
                "Get hover information (type, signature, documentation) for a symbol "
                "at a specific position in a file. Returns the same info you'd see "
                "when hovering over a symbol in VS Code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to repo root e.g. src/handler.cpp"
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number (0-based) e.g. 42 for line 43"
                    },
                    "col": {
                        "type": "integer",
                        "description": "Column number (0-based) e.g. 10"
                    },
                    "root": {
                        "type": "string",
                        "description": "Repo root path"
                    }
                },
                "required": ["path", "line", "col", "root"]
            }
        ),
        types.Tool(
            name="get_incoming_calls",
            description=(
                "Get incoming call hierarchy (callers) for a C++ function. "
                "Returns all functions/methods that call the specified function. "
                "Use to understand who calls this function."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Function name or ClassName::methodName e.g. Parser::parse"
                    },
                    "root": {
                        "type": "string",
                        "description": "Repo root path"
                    }
                },
                "required": ["name", "root"]
            }
        ),
    ]

def _run_tool(name: str, arguments: dict):
    """The actual (blocking) query work. Runs in a worker thread via
    asyncio.to_thread (see call_tool below) so the event loop stays free to
    accept and dispatch other concurrent tool calls instead of blocking on
    this one's clangd round-trips -- ClangdClient's request dispatch is
    already thread-safe (that's what the pipelined class-query relies on),
    so multiple tool calls can share the same warm daemon concurrently."""
    root = arguments.get("root")
    if not root:
        return "Error: root path is required"

    try:
        client = get_client(root)
    except Exception as e:
        return "Error starting clangd: %s" % e

    # A missing compile_commands.json isn't an exception -- clangd starts
    # fine, it just has nothing to index, so every query below will quietly
    # come back empty forever. Callers have no other way to tell "no
    # matches" apart from "no index", so say so up front.
    warning = ""
    if not client._db_found:
        warning = (
            "# Warning: no compile_commands.json found under %r (checked "
            "that path and its build/ subdir) -- clangd has no index, so "
            "results below are likely empty regardless of the query.\n\n"
            % root
        )

    try:
        if name in ("get_function_info", "get_class_info", "get_macro_info", "get_incoming_calls"):
            sym_name = arguments.get("name")
            if not sym_name:
                return warning + "Error: name is required"

            if name == "get_function_info":
                result = run_query_as_string(client, arguments.get("mode", "all"), sym_name, 120)
            elif name == "get_class_info":
                result = run_class_query_as_string(client, arguments.get("mode", "all"), sym_name, 120)
            elif name == "get_macro_info":
                result = run_macro_query_as_string(client, arguments.get("mode", "all"), sym_name, 120)
            else:  # get_incoming_calls
                result = run_incoming_calls_query_as_string(client, sym_name, 120)

        elif name == "get_struct_info":
            sym_name = arguments.get("name")
            if not sym_name:
                return warning + "Error: name is required"
            result = run_struct_query_as_string(
                client, sym_name, 120, include_decl=arguments.get("include_decl", False)
            )

        elif name == "get_hover_info":
            path = arguments.get("path")
            line = arguments.get("line")
            col = arguments.get("col")
            if not path:
                return warning + "Error: path is required"
            if not isinstance(line, int) or not isinstance(col, int):
                return warning + "Error: line and col must be integers (got line=%r, col=%r)" % (line, col)
            result = run_hover_query_as_string(client, path, line, col, wait=10)

        else:
            return warning + "Unknown tool: %s" % name

        return warning + result

    except Exception as e:
        return warning + "Error running query: %s" % e

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    result = await asyncio.to_thread(_run_tool, name, arguments)
    return [types.TextContent(type="text", text=result)]

async def main():
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    except Exception as e:
        print("clangq_mcp main() crashed: %s" % e, file=sys.stderr)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("clangq_mcp asyncio.run() crashed: %s" % e, file=sys.stderr)
