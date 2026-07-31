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
import re
import threading
import subprocess
import pathlib

# ============================ tunables ============================
# Once workspace/symbol has answered anything the index is serving, so a
# further miss is almost certainly a typo. Callers pass long deadlines (the
# MCP server uses 120s) for cold starts; cap a warm miss at this instead.
WARM_MISS_DEADLINE_S = 5

# clangd only reports a macro once its defining file is OPEN, and nothing in
# the protocol says which file that is -- so on a miss we locate the #define
# ourselves. These bound that scan on a large repo.
MACRO_SCAN_EXTS = (".h", ".hpp", ".hh", ".hxx", ".inc", ".ipp", ".def",
                   ".c", ".cc", ".cpp", ".cxx")
MACRO_SCAN_SKIP_DIRS = (".git", ".cache", ".svn", ".hg", "build", "out",
                        "node_modules", "__pycache__")
MACRO_SCAN_MAX_FILES = 2000
MACRO_SCAN_MAX_BYTES = 1 * 1024 * 1024

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
        self._index_warm = False  # set once workspace/symbol returns anything

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
        """Send a request, return a handle, don't wait.

        Callers can fire many before awaiting any; clangd then works on them
        concurrently. The reader demultiplexes replies by id, so arrival
        order doesn't matter.
        """
        rid = self._next_id()
        ev = threading.Event()
        holder = {}
        with self._pending_lock:
            self._pending[rid] = (ev, holder)
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except Exception as e:
            # clangd likely died: drop the pending entry (it will never get a
            # reply) and raise something clearer than a raw BrokenPipeError.
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise RuntimeError("failed to send %s to clangd (process may have exited): %s" % (method, e))
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
        raw_len = headers.get("content-length")
        if raw_len is None:
            raise RuntimeError("clangd sent a message with no Content-Length header")
        try:
            length = int(raw_len)
        except ValueError:
            raise RuntimeError("clangd sent a bad Content-Length: %r" % raw_len)
        body = self._read_exactly(length)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))

    def _fail_all_pending(self, reason):
        """Wake every waiter with an error rather than letting it hang to
        the timeout. Called when the reader loop ends."""
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
                if not isinstance(msg, dict):
                    continue
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
                    sys.stderr.write("[clangd] " + (msg.get("params") or {}).get("message", "") + "\n")
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

    def is_alive(self):
        """Whether clangd is still running. Callers keeping a warm client
        must check before reuse: once it exits, every later request fails."""
        return self.proc is not None and self.proc.poll() is None

    def _uri(self, path):
        return pathlib.Path(path).resolve().as_uri()

    def did_open(self, path):
        uri = self._uri(path)
        if uri in self._opened:
            return uri
        # Every query entry point funnels through here, so a bad path
        # (typo, stale location from the index, a directory) would
        # otherwise surface as a bare OSError with no indication of which
        # file or which query caused it.
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            raise RuntimeError("cannot open source file %r: %s" % (path, e))
        self._notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "cpp", "version": 1, "text": text},
        })
        self._opened.add(uri)
        return uri

    def prime_index(self):
        """Open one source file from the compile DB.

        clangd only loads the DB and starts background indexing once a file
        is opened -- the handshake alone leaves workspace/symbol empty.
        """
        if not self._db_found:
            return None
        try:
            with open(self._db_path, "r", encoding="utf-8", errors="replace") as f:
                entries = json.load(f)
        except Exception as e:
            if self.log:
                sys.stderr.write("[clangq] could not read compile DB: %r\n" % e)
            return None
        if not isinstance(entries, list):
            if self.log:
                sys.stderr.write("[clangq] compile DB is not a JSON array: %s\n" % self._db_path)
            return None
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            fpath = ent.get("file")
            if not fpath:
                continue
            if not os.path.isabs(fpath):
                fpath = os.path.normpath(os.path.join(ent.get("directory") or self.root, fpath))
            if os.path.exists(fpath):
                if self.log:
                    sys.stderr.write("[clangq] priming index by opening %s\n" % fpath)
                # Priming is best-effort: one unreadable entry shouldn't
                # abort startup, since a later entry may well open fine.
                try:
                    self.did_open(fpath)
                except Exception as e:
                    if self.log:
                        sys.stderr.write("[clangq] could not prime with %s (%s)\n" % (fpath, e))
                    continue
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
            if not isinstance(n, dict):
                continue
            if ((n.get("range") or {}).get("start") or {}).get("line") == start_line:
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
            sel_start = sel.get("start") or {}
            rng_end = rng.get("end") or {}
            start_line = sel_start.get("line") or 0
            out.append({
                "name": child.get("name"),
                "start_line": start_line,
                "start_col": sel_start.get("character") or 0,
                "end_line": rng_end.get("line", start_line),
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
        end_line = ((class_node.get("range") or {}).get("end") or {}).get("line")
        return end_line, self._find_class_methods(symbols, class_start)

    def end_line_from_document_symbol(self, symbols, start_line):
        """Given a documentSymbol result (from document_symbol/
        document_symbol_async), find the end line of the node whose range
        starts at start_line."""
        node = self._find_node_by_range_start(symbols or [], start_line)
        if node is None:
            return None
        return ((node.get("range") or {}).get("end") or {}).get("line")

    def workspace_symbol(self, query):
        """Resolve a name via clangd's own index. Returns SymbolInformation[]."""
        syms = self._request("workspace/symbol", {"query": query}) or []
        if syms:
            self._index_warm = True
        return syms

    def resolve_symbol(self, name, deadline_s=10, interval_s=2):
        """Poll workspace/symbol until it hits or the deadline passes.

        The index fills in over time, so a cold start needs retries. The full
        deadline applies only while it might still be cold; see
        WARM_MISS_DEADLINE_S.
        """
        import time
        if getattr(self, "_index_warm", False):
            deadline_s = min(deadline_s, WARM_MISS_DEADLINE_S)
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
        if getattr(self, "_index_warm", False):
            deadline_s = min(deadline_s, WARM_MISS_DEADLINE_S)
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
    def find_macro_definition_file(self, name):
        """Find the file defining macro `name` under the repo root.

        A fallback locator only (see resolve_macro): it tells clangd which
        file to open. Every actual answer still comes from clangd.
        """
        pattern = re.compile(r"^\s*#\s*define\s+" + re.escape(name) + r"\b")
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if d not in MACRO_SCAN_SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if not fn.endswith(MACRO_SCAN_EXTS):
                    continue
                scanned += 1
                if scanned > MACRO_SCAN_MAX_FILES:
                    return None
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(full) > MACRO_SCAN_MAX_BYTES:
                        continue
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if pattern.match(line):
                                return full
                except OSError:
                    continue
        return None

    def resolve_macro(self, name, deadline_s=10, interval_s=2):
        """Resolve a macro. Returns SymbolInformation[] of kind=15.

        clangd indexes #defines as kind=15 (Constant), not 14 (Macro), and
        only reports one once its defining file is open -- the background
        index never surfaces it. So on a miss: locate the #define, open that
        file, ask again. Otherwise we burn the deadline and wrongly answer
        "no macro found".
        """
        import time
        end = time.time() + deadline_s
        attempt = 0
        tried_fallback = False
        while True:
            syms = self.workspace_symbol(name)
            macros = [s for s in syms if s.get("kind") == 15]
            if macros:
                return macros

            if not tried_fallback:
                tried_fallback = True
                path = self.find_macro_definition_file(name)
                if path:
                    if self.log:
                        sys.stderr.write("[clangq] opening %s so clangd can see macro %s\n"
                                         % (path, name))
                    try:
                        self.did_open(path)
                        continue  # re-ask immediately, no need to wait out the interval
                    except Exception as e:
                        if self.log:
                            sys.stderr.write("[clangq] could not open %s (%s)\n" % (path, e))
                else:
                    # Nothing defines it under the root and clangd has no
                    # record: waiting out the deadline cannot change that.
                    if self.log:
                        sys.stderr.write("[clangq] no '#define %s' found under %s\n"
                                         % (name, self.root))
                    return []

            if time.time() >= end:
                return []
            attempt += 1
            if self.log:
                sys.stderr.write("[clangq] no macro match yet, index warming (retry %d)...\n" % attempt)
            time.sleep(interval_s)

    def shutdown(self, timeout=10):
        """Stop clangd and release its process and pipes.

        A long-lived server replaces clients as they die, so leaks accumulate:
        without wait() the child stays a zombie, without close() the fds leak.
        """
        if self.proc is None:
            return
        try:
            self._request("shutdown", None, timeout=timeout)
            self._notify("exit", {})
        except Exception:
            pass

        # Let it exit on its own, then insist.
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                except Exception:
                    pass

        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

        # Nothing will get a reply now.
        self._fail_all_pending("clangd shut down")
