import asyncio
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

def get_client(root):
    if root not in _clients:
        _clients[root] = ClangdClient(root).start()
        _clients[root].prime_index()
    return _clients[root]

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

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    root = arguments.get("root")
    if not root:
        return [types.TextContent(type="text", text="Error: root path is required")]

    try:
        client = get_client(root)
    except Exception as e:
        return [types.TextContent(type="text", text="Error starting clangd: %s" % e)]

    try:
        if name == "get_function_info":
            result = run_query_as_string(
                client,
                arguments.get("mode", "all"),
                arguments["name"],
                120
            )

        elif name == "get_class_info":
            result = run_class_query_as_string(
                client,
                arguments.get("mode", "all"),
                arguments["name"],
                120
            )

        elif name == "get_macro_info":
            result = run_macro_query_as_string(
                client,
                arguments.get("mode", "all"),
                arguments["name"],
                120
            )

        elif name == "get_struct_info":
            result = run_struct_query_as_string(
                client,
                arguments["name"],
                120,
                include_decl=arguments.get("include_decl", False)
            )

        elif name == "get_hover_info":
            result = run_hover_query_as_string(
                client,
                arguments["path"],
                arguments["line"],
                arguments["col"],
                wait=10
            )

        elif name == "get_incoming_calls":
            result = run_incoming_calls_query_as_string(
                client,
                arguments["name"],
                120
            )

        else:
            result = "Unknown tool: %s" % name

    except Exception as e:
        result = "Error running query: %s" % e

    return [types.TextContent(type="text", text=result)]

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
