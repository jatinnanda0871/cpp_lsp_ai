#!/usr/bin/python3
"""
Standalone semantic-query CLI built only on clangd. No code.db, no ripgrep,
no dependency on index.py / query.py.

A background daemon holds clangd warm so the index is built ONCE and reused
across many separate invocations:

  ./clangq.py all     processRequest  --root /path/to/repo   # auto-starts daemon, queries
  ./clangq.py refs    handleX         --root /path/to/repo   # reuses the same warm daemon
  ./clangq.py callers doThing         --root /path/to/repo
  ./clangq.py stop                    --root /path/to/repo   # shut the daemon down

Other modes:
  ./clangq.py daemon  --root /path/to/repo   # run the daemon in the foreground (watch -v logs)
  ./clangq.py shell   --root /path/to/repo   # interactive REPL in one process
  add --no-daemon to any query to run it in-process (old one-shot behaviour)

compile_commands.json is auto-discovered in <root> then <root>/build (override --ccdir).
First query is slow (clangd indexes); the daemon keeps it warm thereafter.
"""
import sys
import os
import io
import time
import json
import socket
import hashlib
import tempfile
import getpass
import argparse
import subprocess
from urllib.parse import urlparse, unquote

from clangd_client import ClangdClient

# ============================= formatting helpers =============================
def uri_to_path(uri):
    p = unquote(urlparse(uri).path)
    if os.name == "nt" and len(p) >= 3 and p[0] == "/" and p[2] == ":":
        p = p[1:]  # file:///C:/... -> C:/... (urlparse leaves the leading slash)
    try:
        return os.path.relpath(p)
    except Exception:
        return p

def loc_str(loc):
    line = loc.get("range", {}).get("start", {}).get("line", 0) + 1
    return "%s:%d" % (uri_to_path(loc.get("uri", "")), line)

def item_str(item):
    return "%-30s %s" % (item.get("name", "?"), loc_str(item))

def pick_symbol_kind(symbols, name, kind):
    exact = [s for s in symbols if s.get("name") == name and s.get("kind") == kind]
    return (exact or symbols)[0]

# ============================= query logic (writes to `out`) =============================
def resolve_position(client, name, wait, verbose):
    symbols = client.resolve_symbol(name, deadline_s=wait)
    if not symbols:
        return []

    # Get all exact matches
    exact = [s for s in symbols if s.get("name") == name]
    symbols = exact if exact else symbols

    if len(symbols) > 1 and verbose:
        sys.stderr.write("note: %d matches for %r\n" % (len(symbols), name))
        for s in symbols:
            sys.stderr.write("  %s\n" % item_str(s))

    results = []
    for sym in symbols:
        loc = sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        if path and not os.path.exists(path):
            path = os.path.join(client.root, path)
        start = (loc.get("range") or {}).get("start", {"line": 0, "character": 0})
        end = client.get_function_end_line(path, start.get("line", 0))

        # For getting function declaration info
        decl = client.declaration(path, start.get("line", 0), start.get("character", 0))
        decl_path = uri_to_path(decl[0].get("uri", "")) if decl else ""
        decl_line = decl[0].get("range", {}).get("start", {}).get("line", 0) if decl else 0

        results.append((path, start.get("line", 0), start.get("character", 0), end, decl_path, decl_line))

    return results

def show_refs(client, name, path, line, col, include_decl, out):
    try:
        refs = client.references(path, line, col, include_decl)
    except Exception as e:
        print("  references: query failed (%s)" % e, file=out)
        return
    print("%d references of %s" % (len(refs), name), file=out)
    for l in refs:
        print("  " + loc_str(l), file=out)

def show_calls(client, name, path, line, col, direction, out):
    """Show call hierarchy (incoming calls only).

    direction must be 'callers' - outgoing calls are not supported by this clangd version.
    """
    if direction != "callers":
        print("  %s: call hierarchy (outgoing) is not supported by this clangd version" % direction, file=out)
        print("    (only incoming calls are available)", file=out)
        return
    try:
        anchors = client.prepare_calls(path, line, col)
    except Exception as e:
        print("  %s: could not resolve call hierarchy (%s)" % (direction, e), file=out)
        return
    if not anchors:
        print("  %s: no definition found at %s:%d" % (direction, path, line + 1), file=out)
        print("    (symbol may be a declaration without a body, a macro, a type, or an overload set)", file=out)
        return
    results = []
    for item in anchors:
        try:
            results += client.incoming(item)
        except Exception as e:
            pass
    print("%d %s of %s" % (len(results), direction, name), file=out)
    for i in results:
        print("  " + item_str(i), file=out)

def run_class_query(client, mode, name, wait, include_decl=False, verbose=False, out=None):
    out = out or sys.stdout
    # Resolve class symbol (kind=5 is Class in LSP)
    try:
        symbols = client.resolve_symbol(name, deadline_s=wait)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return

    sym = pick_symbol_kind(symbols, name, kind=5)
    if sym is None:
        print("  no class found matching %r" % name, file=out)
        return

    loc = sym.get("location") or {}
    path = uri_to_path(loc.get("uri", ""))
    start_line = (loc.get("range") or {}).get("start", {}).get("line", 0)
    # Single documentSymbol fetch covers both the class end line and its methods
    end_line, methods = client.get_class_end_and_methods(path, start_line)
    if end_line is None:
        end_line = start_line
    print("# %s (class decl %s:%d-%d)" % (name, path, start_line+1, end_line+1), file=out)

    if not methods:
        print("  no methods found in class %r" % name, file=out)
        return

    # Each method's identifier position is already known from the class's
    # own child symbols above (fetched once, via documentSymbol's
    # selectionRange -- the identifier token itself, not the whole
    # declaration span). For classes declared in a header with bodies
    # out-of-line in a .cpp, that position is the declaration only -- it has
    # no body, so anchoring refs/callers there directly would make
    # prepareCallHierarchy come back empty. Ask clangd for the definition
    # from that known position instead: one deterministic LSP call, no
    # fuzzy workspace/symbol search on the qualified "Class::method" string
    # (which is what caused the retry-sleep latency previously -- clangd's
    # fuzzy index matches unqualified names and often misses a
    # "::"-qualified query on the first poll).
    for method in methods:
        method_name = name + "::" + method.get("name")
        print("\n--- Class Method: %s ---" % method_name, file=out)
        try:
            decl_path = path  # methods are children of the class, same file
            decl_line = method.get("start_line", 0)
            decl_col = method.get("start_col", 0)

            try:
                defs = client.definition(decl_path, decl_line, decl_col)
            except Exception:
                defs = None
            if defs:
                d0 = defs[0]
                def_path = uri_to_path(d0.get("uri", "")) or decl_path
                def_range = d0.get("range") or {}
                def_line = def_range.get("start", {}).get("line", decl_line)
                def_col = def_range.get("start", {}).get("character", decl_col)
                def_end = def_range.get("end", {}).get("line", def_line)
            else:
                # No separate body found (e.g. pure virtual): fall back to
                # the declaration itself so callers still get something.
                def_path, def_line, def_col = decl_path, decl_line, decl_col
                def_end = method.get("end_line", decl_line)

            print("# %s  (def %s:%d-%d)" % (method_name, def_path, def_line + 1, def_end + 1), file=out)
            print("# %s  (decl %s:%d)" % (method_name, decl_path, decl_line + 1), file=out)
            if mode in ("refs", "all"):
                show_refs(client, method_name, def_path, def_line, def_col, include_decl, out)
            if mode in ("callers", "all"):
                show_calls(client, method_name, def_path, def_line, def_col, "callers", out)
            print("", file=out)
        except Exception as e:
            print("  failed for %s (%s)" % (method_name, e), file=out)

def run_query(client, mode, name, wait, include_decl=False, verbose=False, out=None):
    out = out or sys.stdout
    try:
        pos = resolve_position(client, name, wait, verbose)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return
    if pos is None:
        print("  no symbol matching %r (raise --wait, or check --ccdir)" % name, file=out)
        return
    for path, line, col, end, decl_path, decl_line in pos:
        print("# %s  (def %s:%d-%d)" % (name, path, line + 1, end + 1), file=out)
        print("# %s  (decl %s:%d)" % (name, decl_path, decl_line + 1), file=out)
        if mode in ("refs", "all"):
            show_refs(client, name, path, line, col, include_decl, out)
        if mode in ("callers", "all"):
            show_calls(client, name, path, line, col, "callers", out)
        print("", file=out)

# ========================= Wrapper for MCP =============================
def run_query_as_string(client, mode, name, wait, include_decl=False, verbose=False):
    out = io.StringIO()
    run_query(client, mode, name, wait, include_decl, verbose, out=out)
    return out.getvalue()

def run_class_query_as_string(client, mode, name, wait, include_decl=False, verbose=False):
    out = io.StringIO()
    run_class_query(client, mode, name, wait, include_decl, verbose, out=out)
    return out.getvalue()

# ===================== macro query logic ==========================
def run_macro_query(client, mode, name, wait, include_decl=False, verbose=False, out=None):
    """Query macro information: declaration location and all usages."""
    out = out or sys.stdout
    try:
        macros = client.resolve_macro(name, deadline_s=wait)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return

    if not macros:
        print("  no macro found matching %r (index may still be warming)" % name, file=out)
        return

    if len(macros) > 1 and verbose:
        sys.stderr.write("note: %d macro matches for %r\n" % (len(macros), name))
        for m in macros:
            sys.stderr.write("  %s\n" % item_str(m))

    for macro_sym in macros:
        loc = macro_sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        start = (loc.get("range") or {}).get("start") or {"line": 0, "character": 0}
        line = start.get("line", 0)
        col = start.get("character", 0)

        print("# %s (macro decl %s:%d)" % (name, path, line + 1), file=out)

        # Show the macro detail if available
        detail = macro_sym.get("detail", "")
        if detail:
            print("#   detail: %s" % detail, file=out)

        # Single query for all modes - macros use references for both refs and callers
        try:
            refs = client.references(path, line, col, include_decl)
            if mode in ("refs", "all"):
                print("%d references of %s" % (len(refs), name), file=out)
                for r in refs:
                    print("  " + loc_str(r), file=out)

            if mode in ("callers", "all"):
                print("%d usages (expansions) of %s" % (len(refs), name), file=out)
                for r in refs:
                    print("  " + loc_str(r), file=out)
        except Exception as e:
            print("  references: query failed (%s)" % e, file=out)

        print("", file=out)

def run_macro_query_as_string(client, mode, name, wait, include_decl=False, verbose=False):
    out = io.StringIO()
    run_macro_query(client, mode, name, wait, include_decl, verbose, out=out)
    return out.getvalue()

# ===================== struct query logic ==========================
def run_struct_query(client, name, wait, include_decl=False, verbose=False, out=None):
    """Query struct information: definition location only."""
    out = out or sys.stdout
    try:
        structs = client.resolve_struct(name, deadline_s=wait)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return

    if not structs:
        print("  no struct found matching %r (index may still be warming)" % name, file=out)
        return

    if len(structs) > 1 and verbose:
        sys.stderr.write("note: %d struct matches for %r\n" % (len(structs), name))
        for s in structs:
            sys.stderr.write("  %s\n" % item_str(s))

    for struct_sym in structs:
        loc = struct_sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        start = (loc.get("range") or {}).get("start") or {"line": 0, "character": 0}
        line = start.get("line", 0)
        col = start.get("character", 0)

        print("# %s (struct decl %s:%d)" % (name, path, line + 1), file=out)

        # Show the struct detail if available
        detail = struct_sym.get("detail", "")
        if detail:
            print("#   detail: %s" % detail, file=out)

        print("", file=out)

def run_struct_query_as_string(client, name, wait, include_decl=False, verbose=False):
    out = io.StringIO()
    run_struct_query(client, name, wait, include_decl=include_decl, verbose=verbose, out=out)
    return out.getvalue()

# ===================== hover query logic ==========================
def run_hover_query(client, path, line, col, wait=10, verbose=False, out=None):
    """Get hover information for a symbol at a specific position.

    Args:
        client: ClangdClient instance
        path: File path
        line: Line number (0-based)
        col: Column number (0-based)
        wait: Timeout for waiting on index
        verbose: Print debug info to stderr
        out: Output file-like object
    """
    out = out or sys.stdout

    try:
        hover = client.hover(path, line, col)
    except Exception as e:
        print("  hover query failed (%s)" % e, file=out)
        return

    if not hover:
        print("  no hover information available at %s:%d" % (path, line + 1), file=out)
        return

    # Extract and format hover content
    contents = hover.get("contents", "")
    range_info = hover.get("range")

    # Get location info
    loc_path = uri_to_path(path)

    if isinstance(contents, dict):
        # Could be a markedString dict with language and value
        language = contents.get("language", "")
        value = contents.get("value", "")
        if value:
            print("# Hover at %s:%d" % (loc_path, line + 1), file=out)
            if language:
                print("# Language: %s" % language, file=out)
            print("# Content:\n%s" % value, file=out)
        else:
            # Could be a DocumentationFormat dict
            print("# Hover at %s:%d" % (loc_path, line + 1), file=out)
            print("# Contents: %s" % json.dumps(contents, indent=2), file=out)
    elif isinstance(contents, list):
        # Array of markedStrings or MarkupContent
        print("# Hover at %s:%d" % (loc_path, line + 1), file=out)
        parts = []
        for item in contents:
            if isinstance(item, dict):
                if "value" in item:
                    parts.append(item.get("value", ""))
                elif "language" in item:
                    parts.append("```%s\n%s\n```" % (item.get("language", ""), item.get("value", "")))
            else:
                parts.append(str(item))
        print("# Content:\n%s" % "\n".join(parts), file=out)
    elif isinstance(contents, str):
        # Plain string content (may be markdown)
        print("# Hover at %s:%d" % (loc_path, line + 1), file=out)
        if range_info:
            start = range_info.get("start", {})
            end = range_info.get("end", {})
            print("# Symbol range: line %d-%d" % (start.get("line", 0) + 1, end.get("line", 0) + 1), file=out)
        print("# Content:\n%s" % contents, file=out)
    else:
        print("# Hover at %s:%d" % (loc_path, line + 1), file=out)
        print("# Contents: %s" % str(contents), file=out)

def run_hover_query_as_string(client, path, line, col, wait=10, verbose=False):
    out = io.StringIO()
    run_hover_query(client, path, line, col, wait=wait, verbose=verbose, out=out)
    return out.getvalue()

def run_incoming_calls_query(client, name, wait, verbose=False, out=None):
    """Get incoming calls (callers) for a function.

    Resolves the function symbol position and queries for callers.
    """
    out = out or sys.stdout
    try:
        pos = resolve_position(client, name, wait, verbose)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return
    if not pos:
        print("  no definition found for %r" % name, file=out)
        return
    for path, line, col, end, decl_path, decl_line in pos:
        show_calls(client, name, path, line, col, "callers", out)

def run_incoming_calls_query_as_string(client, name, wait=120, verbose=False):
    out = io.StringIO()
    run_incoming_calls_query(client, name, wait, verbose=verbose, out=out)
    return out.getvalue()

# ===================== daemon transport =====================
def _user():
    try:
        return getpass.getuser()
    except Exception:
        return "u"

def _paths(root):
    key = hashlib.sha1(os.path.abspath(root).encode()).hexdigest()[:12]
    base = os.path.join(tempfile.gettempdir(), "clangq-%s-%s" % (_user(), key))
    return base + ".sock", base + ".log"

def _send_json(conn, obj):
    conn.sendall(json.dumps(obj).encode("utf-8") + b"\n")

def _recv_line(conn):
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8").strip()

def _try_connect(sock_path):
    if not os.path.exists(sock_path):
        return None
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.connect(sock_path)
        return c
    except OSError:
        c.close()
        return None

def _ping(sock_path):
    c = _try_connect(sock_path)
    if not c:
        return False
    try:
        _send_json(c, {"cmd": "ping"})
        return bool(json.loads(_recv_line(c)).get("ok"))
    except Exception:
        return False
    finally:
        c.close()

def _wait_connect(sock_path, timeout):
    end = time.time() + timeout
    while time.time() < end:
        c = _try_connect(sock_path)
        if c:
            return c
        time.sleep(0.2)
    return None

# ===================== daemon server =====================
def serve(args):
    sock_path, _ = _paths(args.root)
    if _ping(sock_path):
        if args.verbose:
            sys.stderr.write("[daemon] already running at %s\n" % sock_path)
        return
    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(sock_path)
    except OSError as e:
        sys.stderr.write("[daemon] cannot bind %s (%s)\n" % (sock_path, e))
        return
    srv.listen(16)
    if args.verbose:
        sys.stderr.write("[daemon] listening on %s\n" % sock_path)

    client = ClangdClient(args.root, compile_commands_dir=args.ccdir, clangd=args.clangd,
                           request_timeout=args.req_timeout, log=args.verbose).start()
    client.prime_index()
    if args.verbose:
        sys.stderr.write("[daemon] clangd started once; index priming; ready for queries\n")

    try:
        while True:
            conn, _ = srv.accept()
            try:
                line = _recv_line(conn)
                if not line:
                    continue
                req = json.loads(line)
                cmd = req.get("cmd")
                if cmd == "ping":
                    _send_json(conn, {"ok": True})
                elif cmd == "shutdown":
                    _send_json(conn, {"ok": True})
                    break
                elif cmd == "query":
                    buf = io.StringIO()
                    try:
                        if req.get("is_class", False):
                            run_class_query(client, req["mode"], req["name"], req.get("wait", 10),
                                             req.get("include_decl", False), args.verbose, out=buf)
                        else:
                            run_query(client, req["mode"], req["name"], req.get("wait", 10),
                                       req.get("include_decl", False), args.verbose, out=buf)
                        _send_json(conn, {"out": buf.getvalue()})
                    except Exception as e:
                        _send_json(conn, {"error": repr(e)})
                else:
                    _send_json(conn, {"error": "unknown cmd %r" % cmd})
            finally:
                conn.close()
    finally:
        client.shutdown()
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        if args.verbose:
            sys.stderr.write("[daemon] stopped\n")

def _start_daemon(args, log_path):
    if args.verbose:
        sys.stderr.write("[clangq] no daemon running; starting one (log: %s)\n" % log_path)
    cmd = [sys.executable, os.path.abspath(__file__), "daemon",
           "--root", os.path.abspath(args.root),
           "--wait", str(args.wait),
           "--clangd", args.clangd,
           "--req-timeout", str(args.req_timeout)]
    if args.ccdir:
        cmd += ["--ccdir", args.ccdir]
    if args.verbose:
        cmd += ["-v"]
    logf = open(log_path, "a")
    subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                      start_new_session=True)

def daemon_query(args):
    sock_path, log_path = _paths(args.root)
    conn = _try_connect(sock_path)
    if conn is None and not args.no_daemon:
        _start_daemon(args, log_path)
        conn = _wait_connect(sock_path, 30)
    if conn is None:
        sys.exit("could not connect to a daemon for %s (log: %s)" % (os.path.abspath(args.root), log_path))
    try:
        _send_json(conn, {"cmd": "query", "mode": args.mode, "name": args.name,
                           "wait": args.wait, "include_decl": args.include_decl, "is_class": args.is_class})
        resp = json.loads(_recv_line(conn))
    finally:
        conn.close()
    if "error" in resp:
        print("  daemon error:", resp["error"])
    else:
        sys.stdout.write(resp.get("out", ""))

def stop(args):
    sock_path, _ = _paths(args.root)
    c = _try_connect(sock_path)
    if not c:
        print("no daemon running for %s" % os.path.abspath(args.root))
        return
    try:
        _send_json(c, {"cmd": "shutdown"})
        json.loads(_recv_line(c))
        print("daemon stopped")
    finally:
        c.close()

# ===================== interactive shell (single process) ===========
HELP = ("commands:\n"
        "  refs <symbol> [decl]     cross-file references ('decl' includes the declaration)\n"
        "  callers <symbol>         incoming calls\n"
        "  all <symbol>             refs + callers\n"
        "  help                     this message\n"
        "  quit | exit              shut down clangd and leave")

def repl(client, wait, verbose):
    print("clangq interactive - clangd stays warm across queries. Type 'help' or 'quit'.")
    while True:
        try:
            line = input("clangq> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print("\n(use 'quit' to exit)")
            continue
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        if cmd in ("quit", "exit", "q"):
            break
        if cmd in ("help", "h", "?"):
            print(HELP)
            continue
        if cmd not in ("refs", "callers", "all"):
            print("  unknown command: %s (try 'help')" % cmd)
            continue
        if len(parts) < 2:
            print("  usage: %s <symbol>" % cmd)
            continue
        name = parts[1]
        include_decl = (cmd == "refs" and len(parts) > 2 and
                        parts[2].lower() in ("decl", "--include-decl"))
        try:
            run_query(client, cmd, name, wait, include_decl, verbose)
        except Exception as e:
            print("  error: %s" % e)

# ===================== entrypoint =====================
def main():
    ap = argparse.ArgumentParser(description="Semantic codebase queries via clangd (standalone, daemon-backed)")
    ap.add_argument("mode", choices=["refs", "callers", "all", "shell", "daemon", "stop"])
    ap.add_argument("name", nargs="?", help="symbol name (omit for shell/daemon/stop)")
    ap.add_argument("--class", action="store_true", dest="is_class", help="treat name as a class instead of a function")
    ap.add_argument("--macro", action="store_true", dest="is_macro", help="treat name as a macro instead of a function")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--ccdir", default=None, help="dir with compile_commands.json (default: <root> then <root>/build)")
    ap.add_argument("--wait", type=int, default=10, help="seconds to wait for the background index")
    ap.add_argument("--req-timeout", type=int, default=10, help="per-request timeout (raise for heavy TUs)")
    ap.add_argument("--clangd", default="clangd", help="clangd binary name/path (e.g. clangd-14)")
    ap.add_argument("--include-decl", action="store_true", help="refs mode: include the declaration")
    ap.add_argument("--no-daemon", action="store_true", help="run the query in-process instead of via the daemon")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.mode == "daemon":
        serve(args)
        return
    if args.mode == "stop":
        stop(args)
        return
    if args.mode == "shell":
        client = ClangdClient(args.root, compile_commands_dir=args.ccdir, clangd=args.clangd,
                               request_timeout=args.req_timeout, log=args.verbose).start()
        try:
            client.prime_index()
            repl(client, args.wait, args.verbose)
        finally:
            client.shutdown()
        return

    # query modes
    if not args.name:
        ap.error("a symbol name is required for %r" % args.mode)

    if args.no_daemon:
        client = ClangdClient(args.root, compile_commands_dir=args.ccdir, clangd=args.clangd,
                               request_timeout=args.req_timeout, log=args.verbose).start()
        try:
            client.prime_index()
            if args.is_class:
                run_class_query(client, args.mode, args.name, args.wait, args.include_decl, args.verbose)
            elif args.is_macro:
                run_macro_query(client, args.mode, args.name, args.wait, args.include_decl, args.verbose)
            else:
                run_query(client, args.mode, args.name, args.wait, args.include_decl, args.verbose)
        finally:
            client.shutdown()
    else:
        daemon_query(args)

if __name__ == "__main__":
    main()
