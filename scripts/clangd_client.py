#!/usr/bin/python3
"""
Standalone clangd LSP client. Self-contained: no code.db, no index.py,
no shared imports with the rest of the codebase-RAG project. Only needs
clangd on PATH and a build/compile_commands.json for the target repo.

Speaks LSP over stdio (JSON-RPC + Content-Length framing) and exposes:
  - workspace_symbol(query)      name -> location (clangd's own index)
  - references(path, line, col)  cross-file 'who uses this'
  - callers(path, l, c)          call hierarchy (incoming calls only)
  - hover(path, line, col)       symbol info at position
"""
import sys
import json
import os
import threading
import subprocess
import pathlib

class ClangdClient:
    def __init__(self, root, compile_commands_dir=None, clangd="clangd",
                 background_index=True, log=False, request_timeout=10):
        self.root = str(pathlib.Path(root).resolve())
        self.log = log
        self.req_timeout = request_timeout
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._opened = set()

        args = [clangd, "--background-index" if background_index else "--background-index=0"]
        if compile_commands_dir:
            candidates = [compile_commands_dir]
        else:
            candidates = [self.root, os.path.join(self.root, "build")]  # root first, then build/
        self._db_path = os.path.join(candidates[0], "compile_commands.json")
        self._db_found = False
        for cand in candidates:
            p = os.path.join(cand, "compile_commands.json")
            if os.path.exists(p):
                self._db_path = p
                args.append("--compile-commands-dir=" + str(pathlib.Path(cand).resolve()))
                self._db_found = True
                break
        self._args = args
        self.proc = None

    # ---- framing --------------------------------------------------------
    def _send(self, msg):
        data = json.dumps(msg).encode("utf-8")
        header = ("Content-Length: %d\r\n\r\n" % len(data)).encode("utf-8")
        self.proc.stdin.write(header + data)
        self.proc.stdin.flush()

    def _next_id(self):
        with self._id_lock:
            self._id += 1
            return self._id

    def _request_async(self, method, params):
        """Send a request and return a handle without waiting for the reply.
        Sending never blocks on a response, so callers can fire many
        requests before waiting on any of them -- clangd then works on them
        concurrently instead of one full round-trip at a time. The reader
        thread demultiplexes replies by id regardless of arrival order, so
        this is safe to call repeatedly before any wait_result()."""
        rid = self._next_id()
        ev = threading.Event()
        holder = {}
        with self._pending_lock:
            self._pending[rid] = (ev, holder)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return (ev, holder, method)

    def wait_result(self, handle, timeout=None):
        if timeout is None:
            timeout = self.req_timeout
        ev, holder, method = handle
        if not ev.wait(timeout):
            raise TimeoutError("clangd request timed out: " + method)
        if "error" in holder:
            raise RuntimeError("clangd error on %s: %s" % (method, holder["error"]))
        return holder.get("result")

    def _request(self, method, params, timeout=None):
        return self.wait_result(self._request_async(method, params), timeout)

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ---- reader thread ----------------------------------------------------
    def _read_exactly(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                return None  # stream closed mid-message
            buf += chunk
        return buf

    def _read_one(self):
        headers = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            line = line.decode("utf-8")
            if line in ("\r\n", "\n"):
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers["content-length"])
        body = self._read_exactly(length)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))

    def _fail_all_pending(self, reason):
        """Wake every waiting request with an error instead of letting it
        hang to the timeout. Called when the reader loop ends."""
        with self._pending_lock:
            items = list(self._pending.items())
            self._pending.clear()
        for rid, (ev, holder) in items:
            if "result" not in holder and "error" not in holder:
                holder["error"] = reason
            ev.set()

    def _reader(self):
        reason = "clangd stream closed"
        try:
            while True:
                msg = self._read_one()
                if msg is None:
                    rc = self.proc.poll()
                    reason = "clangd exited (code %s)" % rc if rc is not None else "clangd stream closed"
                    break
                if "id" in msg and ("result" in msg or "error" in msg):
                    with self._pending_lock:
                        entry = self._pending.pop(msg["id"], None)
                    if entry:
                        ev, holder = entry
                        if "error" in msg:
                            holder["error"] = msg["error"]
                        else:
                            holder["result"] = msg.get("result")
                        ev.set()
                    continue
                if "id" in msg and "method" in msg:      # server->client request
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
                    continue
                method = msg.get("method")               # notification
                if method == "$/progress":
                    val = (msg.get("params") or {}).get("value") or {}
                    if self.log:
                        sys.stderr.write("[clangd] %s %s\n" % (val.get("kind"), val.get("title", "")))
                elif method == "window/logMessage" and self.log:
                    sys.stderr.write("[clangd] " + msg["params"].get("message", "") + "\n")
        except Exception as e:
            reason = "reader error: %r" % e
        finally:
            self._fail_all_pending(reason)

    # ---- lifecycle ----------------------------------------------------
    def start(self):
        if self.log:
            sys.stderr.write("[clangq] launching: %s\n" % " ".join(self._args))
            if self._db_found:
                sys.stderr.write("[clangq] compile_commands.json: FOUND at %s\n" % self._db_path)
            else:
                sys.stderr.write("[clangq] compile_commands.json: NOT FOUND at %s\n"
                                  "[clangq]   -> clangd has nothing to index; pass --ccdir <dir with compile_commands.json>\n"
                                  % self._db_path)
        self.proc = subprocess.Popen(
            self._args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=(None if self.log else subprocess.DEVNULL))
        threading.Thread(target=self._reader, daemon=True).start()
        self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": pathlib.Path(self.root).as_uri(),
            "capabilities": {
                "workspace": {"symbol": {}},
                "textDocument": {
                    "references": {},
                    "callHierarchy": {"dynamicRegistration": False},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                },
                "window": {"workDoneProgress": True},
            },
        })
        self._notify("initialized", {})
        return self

    def _uri(self, path):
        return pathlib.Path(path).resolve().as_uri()

    def did_open(self, path):
        uri = self._uri(path)
        if uri in self._opened:
            return uri
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        self._notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "cpp", "version": 1, "text": text},
        })
        self._opened.add(uri)
        return uri

    def prime_index(self):
        """Open one real source file from the compile DB. clangd only loads the
        compilation database and enqueues background indexing once a file is
        opened -- not from the handshake alone. Without this, workspace/symbol
        stays empty and no .cache/clangd index is ever written."""
        if not self._db_found:
            return None
        try:
            with open(self._db_path, "r", encoding="utf-8", errors="replace") as f:
                entries = json.load(f)
        except Exception as e:
            if self.log:
                sys.stderr.write("[clangq] could not read compile DB: %r\n" % e)
            return None
        for ent in entries:
            fpath = ent.get("file")
            if not fpath:
                continue
            if not os.path.isabs(fpath):
                fpath = os.path.normpath(os.path.join(ent.get("directory", self.root), fpath))
            if os.path.exists(fpath):
                if self.log:
                    sys.stderr.write("[clangq] priming index by opening %s\n" % fpath)
                self.did_open(fpath)
                return fpath
        if self.log:
            sys.stderr.write("[clangq] no openable source file found in compile DB\n")
        return None

    # ---- queries --------------------------------------------------------
    def declaration_async(self, path, line, col):
        uri = self.did_open(path)
        return self._request_async("textDocument/declaration", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

    def definition_async(self, path, line, col):
        uri = self.did_open(path)
        return self._request_async("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

    def document_symbol(self, path):
        uri = self.did_open(path)
        return self._request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        }) or []

    def document_symbol_async(self, path):
        uri = self.did_open(path)
        return self._request_async("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })

    def _find_node_by_range_start(self, nodes, start_line):
        """Recursively search a hierarchical DocumentSymbol[] tree for the
        node whose full range starts at start_line (top-level or nested,
        e.g. a method inside a class)."""
        for n in nodes:
            if (n.get("range") or {}).get("start", {}).get("line") == start_line:
                return n
            found = self._find_node_by_range_start(n.get("children") or [], start_line)
            if found is not None:
                return found
        return None

    def _find_class_methods(self, symbols, class_start):
        class_node = self._find_node_by_range_start(symbols, class_start)
        if class_node is None:
            return []
        out = []
        for child in class_node.get("children") or []:
            if child.get("kind") not in (6, 12):  # Method or Function
                continue
            # selectionRange is the identifier token itself; range is the
            # whole declaration (from the return type). Definition/reference
            # lookups need a position on the identifier, so use selectionRange.
            sel = child.get("selectionRange") or child.get("range") or {}
            rng = child.get("range") or {}
            out.append({
                "name": child.get("name"),
                "start_line": sel.get("start", {}).get("line", 0),
                "start_col": sel.get("start", {}).get("character", 0),
                "end_line": rng.get("end", {}).get("line", sel.get("start", {}).get("line", 0)),
            })
        return out

    def get_class_end_and_methods(self, path, class_start):
        """Single documentSymbol fetch for both the class end line and its
        methods, instead of two separate calls that each re-fetch the same
        document symbol tree."""
        symbols = self.document_symbol(path)
        class_node = self._find_node_by_range_start(symbols, class_start)
        if class_node is None:
            return None, []
        end_line = (class_node.get("range") or {}).get("end", {}).get("line")
        return end_line, self._find_class_methods(symbols, class_start)

    def end_line_from_document_symbol(self, symbols, start_line):
        """Given a documentSymbol result (from document_symbol/
        document_symbol_async), find the end line of the node whose range
        starts at start_line."""
        node = self._find_node_by_range_start(symbols or [], start_line)
        if node is None:
            return None
        return (node.get("range") or {}).get("end", {}).get("line")

    def workspace_symbol(self, query):
        """Resolve a name via clangd's own index. Returns SymbolInformation[]."""
        return self._request("workspace/symbol", {"query": query}) or []

    def resolve_symbol(self, name, deadline_s=10, interval_s=2):
        """Poll workspace/symbol until it returns hits or the deadline passes.
        clangd's index fills in over time, so a cold start needs retries rather
        than relying on a one-shot 'end' event."""
        import time
        end = time.time() + deadline_s
        attempt = 0
        while True:
            syms = self.workspace_symbol(name)
            if syms:
                return syms
            if time.time() >= end:
                return []
            attempt += 1
            if self.log:
                sys.stderr.write("[clangq] no match yet, index warming (retry %d)...\n" % attempt)
            time.sleep(interval_s)

    def references(self, path, line, col, include_decl=False):
        uri = self.did_open(path)
        return self._request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
            "context": {"includeDeclaration": include_decl},
        }) or []

    def references_async(self, path, line, col, include_decl=False):
        uri = self.did_open(path)
        return self._request_async("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
            "context": {"includeDeclaration": include_decl},
        })

    def prepare_calls_async(self, path, line, col):
        """Resolve the call-hierarchy anchor(s) at a position. Returns a list of
        CallHierarchyItem; empty if clangd can't resolve a callable here (e.g. the
        position is a declaration without a definition, a macro, or a type)."""
        uri = self.did_open(path)
        return self._request_async("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

    def incoming_async(self, item):
        return self._request_async("callHierarchy/incomingCalls", {"item": item})

    # ---- hover queries --------------------------------------------------------
    def hover(self, path, line, col):
        """Get hover information for a symbol at the given position.

        Returns:
            dict with 'contents' (string or markedString array) and 'range' keys,
            or None if no hover info available.
        """
        uri = self.did_open(path)
        return self._request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        }) or None

    # ---- struct queries --------------------------------------------------------
    def resolve_struct(self, name, deadline_s=10, interval_s=2):
        """Resolve a struct name via clangd's workspace/symbol index.
        Returns SymbolInformation[] with kind=13 (Struct) or kind=5 (Class/Struct) matching the name.

        Note: clangd indexes typedef struct as kind=5 (Class), not kind=13 (Struct).
        This method checks both kinds to find struct definitions."""
        import time
        end = time.time() + deadline_s
        attempt = 0
        while True:
            syms = self.workspace_symbol(name)
            # Check for kind=13 (Struct) first, then kind=5 (Class - includes typedef struct)
            structs = [s for s in syms if s.get("kind") in (13, 5)]
            if structs:
                return structs
            if time.time() >= end:
                return []
            attempt += 1
            if self.log:
                sys.stderr.write("[clangq] no struct match yet, index warming (retry %d)...\n" % attempt)
            time.sleep(interval_s)

    # ---- macro queries --------------------------------------------------------
    def resolve_macro(self, name, deadline_s=10, interval_s=2):
        """Resolve a macro name via clangd's workspace/symbol index.
        Returns SymbolInformation[] with kind=15 (Constant) matching the name.

        Note: clangd indexes #define macros as kind=15 (Constant), not kind=14 (Macro)."""
        import time
        end = time.time() + deadline_s
        attempt = 0
        while True:
            syms = self.workspace_symbol(name)
            macros = [s for s in syms if s.get("kind") == 15]
            if macros:
                return macros
            if time.time() >= end:
                return []
            attempt += 1
            if self.log:
                sys.stderr.write("[clangq] no macro match yet, index warming (retry %d)...\n" % attempt)
            time.sleep(interval_s)

    def shutdown(self):
        try:
            self._request("shutdown", None, timeout=10)
            self._notify("exit", {})
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
