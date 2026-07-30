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
    line = (((loc.get("range") or {}).get("start") or {}).get("line") or 0) + 1
    return "%s:%d" % (uri_to_path(loc.get("uri", "")), line)

def item_str(item):
    return "%-30s %s" % (item.get("name", "?"), loc_str(item))

def pick_symbol_kind(symbols, name, kind):
    if not symbols:
        return None
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

    items = []
    for sym in symbols:
        loc = sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        if path and not os.path.exists(path):
            path = os.path.join(client.root, path)
        start = (loc.get("range") or {}).get("start", {"line": 0, "character": 0})
        items.append({
            "path": path,
            "line": start.get("line", 0),
            "col": start.get("character", 0),
        })

    # documentSymbol (for the end line) and declaration() don't depend on
    # each other's results, so fire both for every candidate before waiting
    # on either -- same fire-then-collect pattern used for
    # class queries, just applied here even for the common single-candidate
    # case, since there's no reason those two round-trips should be serial.
    # One unopenable candidate path (a stale index entry, a generated file
    # that no longer exists) shouldn't sink the whole query -- skip it and
    # keep the candidates that do resolve.
    usable = []
    for it in items:
        try:
            it["doc_handle"] = client.document_symbol_async(it["path"])
            it["decl_handle"] = client.declaration_async(it["path"], it["line"], it["col"])
        except Exception as e:
            if verbose:
                sys.stderr.write("note: skipping %s (%s)\n" % (it["path"], e))
            continue
        usable.append(it)

    results = []
    for it in usable:
        try:
            doc_symbols = client.wait_result(it["doc_handle"])
        except Exception:
            doc_symbols = []
        end = client.end_line_from_document_symbol(doc_symbols, it["line"])
        if end is None:
            end = it["line"]

        try:
            decl = client.wait_result(it["decl_handle"])
        except Exception:
            decl = None
        decl_path = uri_to_path(decl[0].get("uri", "")) if decl else ""
        decl_line = 0
        if decl:
            decl_start = (decl[0].get("range") or {}).get("start") or {}
            decl_line = decl_start.get("line") or 0

        results.append((it["path"], it["line"], it["col"], end, decl_path, decl_line))

    return results

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
    start_line = ((loc.get("range") or {}).get("start") or {}).get("line") or 0
    # Single documentSymbol fetch covers both the class end line and its methods
    try:
        end_line, methods = client.get_class_end_and_methods(path, start_line)
    except Exception as e:
        print("  documentSymbol failed for %s (%s)" % (path, e), file=out)
        end_line, methods = None, []
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
    # prepareCallHierarchy come back empty, so we resolve the definition
    # first via textDocument/definition.
    #
    # All of this is pipelined across every method instead of done one
    # method at a time: sending a request doesn't block on its reply, and
    # clangd can work on many read-only queries concurrently, so each phase
    # below fires every method's request before waiting on any of them.
    items = [{
        "method_name": name + "::" + (method.get("name") or "<unnamed>"),
        "decl_path": path,  # methods are children of the class, same file
        "decl_line": method.get("start_line", 0),
        "decl_col": method.get("start_col", 0),
        "decl_end": method.get("end_line", method.get("start_line", 0)),
    } for method in methods]

    # Phase 1: definition() for every method.
    for it in items:
        try:
            it["def_handle"] = client.definition_async(it["decl_path"], it["decl_line"], it["decl_col"])
        except Exception:
            pass
    for it in items:
        try:
            defs = client.wait_result(it["def_handle"]) if "def_handle" in it else None
        except Exception:
            defs = None
        if defs:
            d0 = defs[0]
            def_range = d0.get("range") or {}
            def_start = def_range.get("start") or {}
            def_end = def_range.get("end") or {}
            it["def_path"] = uri_to_path(d0.get("uri", "")) or it["decl_path"]
            it["def_line"] = def_start.get("line", it["decl_line"])
            it["def_col"] = def_start.get("character", it["decl_col"])
            # textDocument/definition's own range is often just the identifier's
            # line, not the whole body -- fall back to that for now, refined
            # below with the full documentSymbol extent (same "end" semantics
            # as a plain get_function_info query).
            it["def_end"] = def_end.get("line", it["def_line"])
        else:
            # No separate body found (e.g. pure virtual): fall back to the
            # declaration itself so callers still get something.
            it["def_path"], it["def_line"], it["def_col"] = it["decl_path"], it["decl_line"], it["decl_col"]
            it["def_end"] = it["decl_end"]

    # Phase 1b: refine def_end to the full body span via documentSymbol on
    # the definition's file -- fetched once per unique file (methods of one
    # class are typically all defined in the same .cpp) rather than once per
    # method, and kept consistent with how a plain get_function_info query
    # reports its "end" line.
    doc_handles = {}
    for it in items:
        if it["def_path"] not in doc_handles:
            try:
                doc_handles[it["def_path"]] = client.document_symbol_async(it["def_path"])
            except Exception:
                # def_end just stays at the coarser definition-range value
                pass
    doc_symbols = {}
    for def_path, handle in doc_handles.items():
        try:
            doc_symbols[def_path] = client.wait_result(handle)
        except Exception:
            doc_symbols[def_path] = []
    for it in items:
        end = client.end_line_from_document_symbol(doc_symbols.get(it["def_path"]), it["def_line"])
        if end is not None:
            it["def_end"] = end

    # Phase 2: references() and prepareCallHierarchy(), anchored on the
    # resolved definition, for every method.
    for it in items:
        if mode in ("refs", "all"):
            try:
                it["refs_handle"] = client.references_async(it["def_path"], it["def_line"], it["def_col"], include_decl)
            except Exception as e:
                it["refs_error"] = e
        if mode in ("callers", "all"):
            try:
                it["calls_handle"] = client.prepare_calls_async(it["def_path"], it["def_line"], it["def_col"])
            except Exception as e:
                it["calls_error"] = e
    for it in items:
        if "refs_handle" in it:
            try:
                it["refs"] = client.wait_result(it["refs_handle"]) or []
            except Exception as e:
                it["refs_error"] = e
        if "calls_handle" in it:
            try:
                it["anchors"] = client.wait_result(it["calls_handle"]) or []
            except Exception as e:
                it["calls_error"] = e

    # Phase 3: incomingCalls() for every anchor found above.
    for it in items:
        if it.get("anchors"):
            it["incoming_handles"] = [client.incoming_async(a) for a in it["anchors"]]
    for it in items:
        if "incoming_handles" not in it:
            continue
        callers = []
        for h in it["incoming_handles"]:
            try:
                callers += [c["from"] for c in (client.wait_result(h) or [])]
            except Exception:
                pass
        it["callers"] = callers

    # Assemble the output, in the original method order.
    for it in items:
        method_name = it["method_name"]
        print("\n--- Class Method: %s ---" % method_name, file=out)
        print("# %s  (def %s:%d-%d)" % (method_name, it["def_path"], it["def_line"] + 1, it["def_end"] + 1), file=out)
        print("# %s  (decl %s:%d)" % (method_name, it["decl_path"], it["decl_line"] + 1), file=out)

        if mode in ("refs", "all"):
            if "refs_error" in it:
                print("  references: query failed (%s)" % it["refs_error"], file=out)
            else:
                refs = it.get("refs") or []
                print("%d references of %s" % (len(refs), method_name), file=out)
                for r in refs:
                    print("  " + loc_str(r), file=out)

        if mode in ("callers", "all"):
            if "calls_error" in it:
                print("  callers: could not resolve call hierarchy (%s)" % it["calls_error"], file=out)
            elif not it.get("anchors"):
                print("  callers: no definition found at %s:%d" % (it["def_path"], it["def_line"] + 1), file=out)
                print("    (symbol may be a declaration without a body, a macro, a type, or an overload set)", file=out)
            else:
                callers = it.get("callers") or []
                print("%d callers of %s" % (len(callers), method_name), file=out)
                for c in callers:
                    print("  " + item_str(c), file=out)
        print("", file=out)

def run_query(client, mode, name, wait, include_decl=False, verbose=False, out=None):
    out = out or sys.stdout
    try:
        pos = resolve_position(client, name, wait, verbose)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return
    if not pos:
        print("  no symbol matching %r (raise --wait, or check --ccdir)" % name, file=out)
        return

    items = [{"path": p, "line": l, "col": c, "end": e, "decl_path": dp, "decl_line": dl}
             for (p, l, c, e, dp, dl) in pos]

    # references() and prepareCallHierarchy() don't depend on each other, so
    # fire both for every position before waiting on either (same pattern as
    # run_class_query -- there's usually just one position here, but the two
    # calls still don't need to be serial).
    for it in items:
        if mode in ("refs", "all"):
            try:
                it["refs_handle"] = client.references_async(it["path"], it["line"], it["col"], include_decl)
            except Exception as e:
                it["refs_error"] = e
        if mode in ("callers", "all"):
            try:
                it["calls_handle"] = client.prepare_calls_async(it["path"], it["line"], it["col"])
            except Exception as e:
                it["calls_error"] = e
    for it in items:
        if "refs_handle" in it:
            try:
                it["refs"] = client.wait_result(it["refs_handle"]) or []
            except Exception as e:
                it["refs_error"] = e
        if "calls_handle" in it:
            try:
                it["anchors"] = client.wait_result(it["calls_handle"]) or []
            except Exception as e:
                it["calls_error"] = e

    for it in items:
        if it.get("anchors"):
            it["incoming_handles"] = [client.incoming_async(a) for a in it["anchors"]]
    for it in items:
        if "incoming_handles" not in it:
            continue
        callers = []
        for h in it["incoming_handles"]:
            try:
                callers += [c["from"] for c in (client.wait_result(h) or [])]
            except Exception:
                pass
        it["callers"] = callers

    for it in items:
        print("# %s  (def %s:%d-%d)" % (name, it["path"], it["line"] + 1, it["end"] + 1), file=out)
        print("# %s  (decl %s:%d)" % (name, it["decl_path"], it["decl_line"] + 1), file=out)

        if mode in ("refs", "all"):
            if "refs_error" in it:
                print("  references: query failed (%s)" % it["refs_error"], file=out)
            else:
                refs = it.get("refs") or []
                print("%d references of %s" % (len(refs), name), file=out)
                for r in refs:
                    print("  " + loc_str(r), file=out)

        if mode in ("callers", "all"):
            if "calls_error" in it:
                print("  callers: could not resolve call hierarchy (%s)" % it["calls_error"], file=out)
            elif not it.get("anchors"):
                print("  callers: no definition found at %s:%d" % (it["path"], it["line"] + 1), file=out)
                print("    (symbol may be a declaration without a body, a macro, a type, or an overload set)", file=out)
            else:
                callers = it.get("callers") or []
                print("%d callers of %s" % (len(callers), name), file=out)
                for c in callers:
                    print("  " + item_str(c), file=out)
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

    items = [{"path": p, "line": l, "col": c} for (p, l, c, _end, _dp, _dl) in pos]
    for it in items:
        try:
            it["calls_handle"] = client.prepare_calls_async(it["path"], it["line"], it["col"])
        except Exception as e:
            it["calls_error"] = e
    for it in items:
        if "calls_handle" in it:
            try:
                it["anchors"] = client.wait_result(it["calls_handle"]) or []
            except Exception as e:
                it["calls_error"] = e
    for it in items:
        if it.get("anchors"):
            it["incoming_handles"] = [client.incoming_async(a) for a in it["anchors"]]
    for it in items:
        if "incoming_handles" not in it:
            continue
        callers = []
        for h in it["incoming_handles"]:
            try:
                callers += [c["from"] for c in (client.wait_result(h) or [])]
            except Exception:
                pass
        it["callers"] = callers

    for it in items:
        if "calls_error" in it:
            print("  callers: could not resolve call hierarchy (%s)" % it["calls_error"], file=out)
        elif not it.get("anchors"):
            print("  callers: no definition found at %s:%d" % (it["path"], it["line"] + 1), file=out)
            print("    (symbol may be a declaration without a body, a macro, a type, or an overload set)", file=out)
        else:
            callers = it.get("callers") or []
            print("%d callers of %s" % (len(callers), name), file=out)
            for c in callers:
                print("  " + item_str(c), file=out)

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
