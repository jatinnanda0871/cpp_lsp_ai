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
import re
import time
import json
import socket
import hashlib
import tempfile
import getpass
import argparse
import threading
import subprocess
from urllib.parse import urlparse, unquote

from clangd_client import ClangdClient

# ============================= formatting helpers =============================
def uri_to_path(uri, base=None):
    """file:// URI -> an ABSOLUTE path.

    Absolute because it goes straight back to clangd and to open(); the MCP
    host picks the server's cwd, so a cwd-relative path would break. Pass
    `base` only to get the display form (see display_path).
    """
    p = unquote(urlparse(uri).path)
    if os.name == "nt" and len(p) >= 3 and p[0] == "/" and p[2] == ":":
        p = p[1:]  # file:///C:/... -> C:/... (urlparse leaves the leading slash)
    if not p:
        return p
    p = os.path.normpath(p)
    if base:
        return display_path(p, base)
    return p

def display_path(path, base=None):
    """Absolute path -> repo-root-relative, for output only.

    Anchored on the root, not the cwd, so a file is always named the same
    way. Files outside the root stay absolute; '../../..' reads worse.
    """
    if not path:
        return path
    if not base:
        return path
    try:
        rel = os.path.relpath(path, base)
    except Exception:
        return path
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return path
    return rel

def client_root(client):
    """Repo root of a client, for anchoring reported paths."""
    return getattr(client, "root", None)

def loc_str(loc, base=None):
    line = (((loc.get("range") or {}).get("start") or {}).get("line") or 0) + 1
    return "%s:%d" % (display_path(uri_to_path(loc.get("uri", "")), base), line)

def item_str(item, base=None):
    return "%-30s %s" % (item.get("name", "?"), loc_str(item, base))

def exact_matches(symbols, name, kind=None):
    """Symbols named exactly `name`.

    Handles 'Class::method': clangd reports those as name='method' plus
    containerName, so a plain compare would call every qualified lookup fuzzy.
    """
    if not isinstance(name, str):
        return []

    def right_kind(s):
        return kind is None or s.get("kind") == kind

    hits = [s for s in symbols if s.get("name") == name and right_kind(s)]
    if hits or "::" not in name:
        return hits

    qualifier, _, leaf = name.rpartition("::")
    out = []
    for s in symbols:
        if s.get("name") != leaf or not right_kind(s):
            continue
        container = s.get("containerName") or ""
        if container == qualifier or container.endswith("::" + qualifier):
            out.append(s)
    return out


# '::' fuzzy-matched 18 symbols, each rendered as a full result block.
MAX_FUZZY_CANDIDATES = 5


def inexact_match_note(symbols, name, total=None):
    """Warning for a fuzzy-only match.

    workspace/symbol answers even for nonsense, in the same format as a real
    hit. Saying so keeps typo-tolerance without presenting a guess as fact.
    """
    found = sorted({s.get("name") or "?" for s in symbols})
    shown = ", ".join(found[:MAX_FUZZY_CANDIDATES])
    if len(found) > MAX_FUZZY_CANDIDATES:
        shown += ", ..."
    note = ("  note: nothing is named %r exactly -- showing clangd's closest "
            "fuzzy match(es): %s" % (name, shown))
    if total and total > len(symbols):
        note += " (%d of %d shown)" % (len(symbols), total)
    return note


def pick_symbol_kind(symbols, name, kind):
    if not symbols:
        return None
    return (exact_matches(symbols, name, kind) or symbols)[0]

# ======================= symbol resolution =======================
def resolve_position(client, name, wait, verbose, notes=None):
    """Resolve `name` to candidate definition positions.

    Returns [(path, line, col, end_line, decl_path, decl_line, found_name)],
    one per candidate -- an overload set legitimately yields several.
    `found_name` is what clangd matched, which differs from `name` on a fuzzy
    hit. `notes`, if a list, collects warnings for the caller to print.
    """
    base = client_root(client)
    symbols = client.resolve_symbol(name, deadline_s=wait)
    if not symbols:
        return []

    exact = exact_matches(symbols, name)
    if exact:
        symbols = exact
    else:
        # Only a fuzzy match: keep a handful so the answer stays readable.
        total = len(symbols)
        symbols = symbols[:MAX_FUZZY_CANDIDATES]
        if notes is not None:
            notes.append(inexact_match_note(symbols, name, total=total))

    if len(symbols) > 1 and verbose:
        sys.stderr.write("note: %d matches for %r\n" % (len(symbols), name))
        for s in symbols:
            sys.stderr.write("  %s\n" % item_str(s, base))

    items = []
    for sym in symbols:
        loc = sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        if path and not os.path.exists(path):
            path = os.path.join(client.root, path)
        start = (loc.get("range") or {}).get("start") or {}
        items.append({
            "path": path,
            "line": start.get("line") or 0,
            "col": start.get("character") or 0,
            "found_name": sym.get("name") or name,
        })

    # documentSymbol and declaration are independent: fire both for every
    # candidate before awaiting either. Skip candidates that won't open (a
    # stale index entry) rather than failing the whole query.
    usable = []
    for it in items:
        try:
            it["doc_handle"] = client.document_symbol_async(it["path"])
            it["decl_handle"] = client.declaration_async(it["path"], it["line"], it["col"])
        except Exception as e:
            if verbose:
                sys.stderr.write("note: skipping %s (%s)\n" % (display_path(it["path"], base), e))
            continue
        usable.append(it)

    results = []
    for it in usable:
        doc_symbols = _await(client, it["doc_handle"], default=[])
        # No node may start exactly here; fall back so end is never None.
        end = client.end_line_from_document_symbol(doc_symbols, it["line"])
        if end is None:
            end = it["line"]

        decl = _await(client, it["decl_handle"], default=None)
        decl_path, decl_line = "", 0
        if decl:
            decl_path = uri_to_path(decl[0].get("uri", ""))
            decl_line = ((decl[0].get("range") or {}).get("start") or {}).get("line") or 0

        results.append((it["path"], it["line"], it["col"], end,
                        decl_path, decl_line, it["found_name"]))

    return results


# ==================== the refs / callers pipeline ====================
# Every symbol query wants the same things at an anchor: references, call
# anchors, and their incoming calls. These run over a LIST of items, firing
# all requests before awaiting any -- clangd serves read-only queries
# concurrently, so N symbols cost ~1 round-trip instead of N.
#
# An item carries it["anchor"] = (path, line, col); these add
# refs / refs_error / anchors / calls_error / callers.

WANTS_REFS = ("refs", "all")
WANTS_CALLERS = ("callers", "all")


def _await(client, handle, default=None):
    """wait_result that turns any failure into `default`."""
    try:
        return client.wait_result(handle)
    except Exception:
        return default


def fetch_refs_and_callers(client, items, mode, include_decl=False):
    """Populate refs and/or callers for every item, per `mode`."""
    for it in items:
        path, line, col = it["anchor"]
        if mode in WANTS_REFS:
            try:
                it["refs_handle"] = client.references_async(path, line, col, include_decl)
            except Exception as e:
                it["refs_error"] = e
        if mode in WANTS_CALLERS:
            try:
                it["calls_handle"] = client.prepare_calls_async(path, line, col)
            except Exception as e:
                it["calls_error"] = e

    for it in items:
        if "refs_handle" in it:
            try:
                it["refs"] = client.wait_result(it.pop("refs_handle")) or []
            except Exception as e:
                it["refs_error"] = e
        if "calls_handle" in it:
            try:
                it["anchors"] = client.wait_result(it.pop("calls_handle")) or []
            except Exception as e:
                it["calls_error"] = e

    if mode in WANTS_CALLERS:
        _fetch_incoming_calls(client, items)


def _fetch_incoming_calls(client, items):
    """Expand each item's call-hierarchy anchors into its list of callers."""
    for it in items:
        if it.get("anchors"):
            try:
                it["incoming_handles"] = [client.incoming_async(a) for a in it["anchors"]]
            except Exception as e:
                it["calls_error"] = e

    for it in items:
        handles = it.pop("incoming_handles", None)
        if not handles:
            continue
        callers = []
        for handle in handles:
            for call in (_await(client, handle, default=[]) or []):
                if isinstance(call, dict) and call.get("from"):
                    callers.append(call["from"])
        it["callers"] = callers


def print_refs_and_callers(it, label, mode, base, out):
    """Render one item's refs/callers sections."""
    if mode in WANTS_REFS:
        if "refs_error" in it:
            print("  references: query failed (%s)" % it["refs_error"], file=out)
        else:
            refs = it.get("refs") or []
            print("%d references of %s" % (len(refs), label), file=out)
            for r in refs:
                print("  " + loc_str(r, base), file=out)

    if mode in WANTS_CALLERS:
        if "calls_error" in it:
            print("  callers: could not resolve call hierarchy (%s)" % it["calls_error"], file=out)
        elif not it.get("anchors"):
            path, line, _col = it["anchor"]
            print("  callers: no definition found at %s:%d"
                  % (display_path(path, base), line + 1), file=out)
            print("    (symbol may be a declaration without a body, a macro, "
                  "a type, or an overload set)", file=out)
        else:
            callers = it.get("callers") or []
            print("%d callers of %s" % (len(callers), label), file=out)
            for c in callers:
                print("  " + item_str(c, base), file=out)


def _resolve_or_report(client, name, wait, verbose, out, empty_message):
    """Front half of every symbol query. Returns (positions, notes); empty
    positions means stop -- the reason has already been printed."""
    notes = []
    try:
        positions = resolve_position(client, name, wait, verbose, notes=notes)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return [], notes
    if not positions:
        print(empty_message % name, file=out)
        return [], notes
    return positions, notes


# ======================= function queries =======================
def run_query(client, mode, name, wait, include_decl=False, verbose=False, out=None):
    """Definition, declaration, references and callers of a function."""
    base = client_root(client)
    out = out or sys.stdout

    positions, notes = _resolve_or_report(
        client, name, wait, verbose, out,
        "  no symbol matching %r (raise --wait, or check --ccdir)")
    if not positions:
        return
    for note in notes:
        print(note, file=out)

    items = [{"anchor": (path, line, col), "end": end,
              "decl_path": decl_path, "decl_line": decl_line,
              # on a fuzzy hit, label with what clangd matched -- the query
              # string titled every result of a '::' search as "::"
              "label": found_name if notes else name}
             for (path, line, col, end, decl_path, decl_line, found_name) in positions]

    fetch_refs_and_callers(client, items, mode, include_decl)

    for it in items:
        path, line, _col = it["anchor"]
        label = it["label"]
        print("# %s  (def %s:%d-%d)"
              % (label, display_path(path, base), line + 1, it["end"] + 1), file=out)
        if it["decl_path"]:
            print("# %s  (decl %s:%d)"
                  % (label, display_path(it["decl_path"], base), it["decl_line"] + 1), file=out)
        else:
            # textDocument/declaration found nothing separate from the
            # definition (common when there is no header declaration) --
            # "(decl :1)" would otherwise read as a real location at line 1
            # of an unnamed file.
            print("# %s  (decl: not found)" % label, file=out)
        print_refs_and_callers(it, label, mode, base, out)
        print("", file=out)


def run_incoming_calls_query(client, name, wait, verbose=False, out=None):
    """Just the callers of a function -- run_query's 'callers' half."""
    base = client_root(client)
    out = out or sys.stdout

    positions, notes = _resolve_or_report(
        client, name, wait, verbose, out, "  no definition found for %r")
    if not positions:
        return
    for note in notes:
        print(note, file=out)

    items = [{"anchor": (path, line, col),
              "label": found_name if notes else name}
             for (path, line, col, _end, _dp, _dl, found_name) in positions]

    fetch_refs_and_callers(client, items, "callers")

    for it in items:
        print_refs_and_callers(it, it["label"], "callers", base, out)


# ========================= implementations =========================
def run_implementations_query(client, name, wait, verbose=False, out=None):
    """Overriding definitions of a virtual method/function -- the answer to
    'who implements this' as opposed to get_incoming_calls's 'who calls this'.
    """
    base = client_root(client)
    out = out or sys.stdout

    positions, notes = _resolve_or_report(
        client, name, wait, verbose, out, "  no symbol matching %r")
    if not positions:
        return
    for note in notes:
        print(note, file=out)

    # Unlike prepareCallHierarchy (which needs a definition with a body),
    # clangd resolves textDocument/implementation from any occurrence of the
    # symbol -- the primary (path, line, col) resolve_position already
    # anchored on, which for a pure virtual with no body IS its declaration.
    # (There is no decl_col to pair with decl_line, so reusing the decl
    # position here would anchor col on the wrong token.)
    items = [{
        "anchor": (path, line, col),
        "label": found_name if notes else name,
    } for (path, line, col, _end, _decl_path, _decl_line, found_name) in positions]

    for it in items:
        p, l, c = it["anchor"]
        try:
            it["impl_handle"] = client.implementation_async(p, l, c)
        except Exception as e:
            it["impl_error"] = e

    for it in items:
        handle = it.pop("impl_handle", None)
        if handle is not None:
            it["impls"] = _await(client, handle, default=[]) or []
        else:
            it["impls"] = []

    # Resolve each hit's enclosing symbol name via one documentSymbol fetch
    # per unique file -- same two-phase batching as _class_method_items.
    all_hits = [(uri_to_path(h.get("uri", "")),
                 ((h.get("range") or {}).get("start") or {}).get("line") or 0)
                for it in items for h in it["impls"]]
    doc_handles = {}
    for hit_path, _line in all_hits:
        if hit_path and hit_path not in doc_handles:
            try:
                doc_handles[hit_path] = client.document_symbol_async(hit_path)
            except Exception:
                pass
    doc_symbols = {p: _await(client, h, default=[]) for p, h in doc_handles.items()}

    def hit_label(hit_path, hit_line):
        node = client._find_node_by_range_start(doc_symbols.get(hit_path) or [], hit_line)
        return node.get("name") if node else None

    for it in items:
        label = it["label"]
        if "impl_error" in it:
            print("  implementations of %s: query failed (%s)" % (label, it["impl_error"]), file=out)
            continue
        impls = it["impls"]
        print("%d implementation%s of %s" % (len(impls), "" if len(impls) == 1 else "s", label), file=out)
        for h in impls:
            hit_path = uri_to_path(h.get("uri", ""))
            hit_line = ((h.get("range") or {}).get("start") or {}).get("line") or 0
            name_hint = hit_label(hit_path, hit_line)
            where = "%s:%d" % (display_path(hit_path, base), hit_line + 1)
            print("  %-30s %s" % (name_hint, where) if name_hint else "  " + where, file=out)


# ========================= class queries =========================
def _class_method_items(client, class_path, methods):
    """A class's child symbols as items anchored on each method's DEFINITION.

    documentSymbol gives the declaration. For a header class with bodies in a
    .cpp that has no body, so prepareCallHierarchy there returns empty --
    resolve the definition first via textDocument/definition.
    """
    items = [{
        "label": None,               # filled in by run_class_query
        "decl_path": class_path,     # methods are children of the class, same file
        "decl_line": m.get("start_line") or 0,
        "decl_col": m.get("start_col") or 0,
        "decl_end": m.get("end_line", m.get("start_line") or 0),
        "name": m.get("name") or "<unnamed>",
    } for m in methods]

    # Phase 1: resolve every method's definition.
    for it in items:
        try:
            it["def_handle"] = client.definition_async(
                it["decl_path"], it["decl_line"], it["decl_col"])
        except Exception:
            pass

    for it in items:
        handle = it.pop("def_handle", None)
        defs = _await(client, handle, default=None) if handle else None
        if defs:
            d0 = defs[0]
            rng = d0.get("range") or {}
            start, end = rng.get("start") or {}, rng.get("end") or {}
            path = uri_to_path(d0.get("uri", "")) or it["decl_path"]
            line = start.get("line", it["decl_line"])
            col = start.get("character", it["decl_col"])
            # definition's range is often just the identifier line; widened below
            it["end"] = end.get("line", line)
        else:
            # No separate body (e.g. pure virtual): fall back to the
            # declaration so the caller still gets something.
            path, line, col = it["decl_path"], it["decl_line"], it["decl_col"]
            it["end"] = it["decl_end"]
        it["anchor"] = (path, line, col)

    # Phase 2: widen each end to the body's extent. One documentSymbol per
    # file -- a class's methods usually share one.
    doc_handles = {}
    for it in items:
        path = it["anchor"][0]
        if path not in doc_handles:
            try:
                doc_handles[path] = client.document_symbol_async(path)
            except Exception:
                pass    # end stays at the coarser definition-range value
    doc_symbols = {path: _await(client, h, default=[])
                   for path, h in doc_handles.items()}
    for it in items:
        path, line, _col = it["anchor"]
        end = client.end_line_from_document_symbol(doc_symbols.get(path), line)
        if end is not None:
            it["end"] = end

    return items


def run_class_query(client, mode, name, wait, include_decl=False, verbose=False, out=None):
    """Declaration of a class plus definition/refs/callers of every method."""
    base = client_root(client)
    out = out or sys.stdout

    try:
        symbols = client.resolve_symbol(name, deadline_s=wait)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return

    sym = pick_symbol_kind(symbols, name, kind=5)   # 5 = Class in LSP
    if sym is None:
        print("  no class found matching %r" % name, file=out)
        return
    if not exact_matches(symbols, name, kind=5):
        print(inexact_match_note(symbols, name), file=out)

    loc = sym.get("location") or {}
    path = uri_to_path(loc.get("uri", ""))
    start_line = ((loc.get("range") or {}).get("start") or {}).get("line") or 0

    # One documentSymbol fetch covers both the class end line and its methods.
    try:
        end_line, methods = client.get_class_end_and_methods(path, start_line)
    except Exception as e:
        print("  documentSymbol failed for %s (%s)" % (display_path(path, base), e), file=out)
        end_line, methods = None, []
    if end_line is None:
        end_line = start_line

    print("# %s (class decl %s:%d-%d)"
          % (name, display_path(path, base), start_line + 1, end_line + 1), file=out)
    if not methods:
        print("  no methods found in class %r" % name, file=out)
        return

    items = _class_method_items(client, path, methods)
    for it in items:
        it["label"] = "%s::%s" % (name, it["name"])

    fetch_refs_and_callers(client, items, mode, include_decl)

    for it in items:
        label = it["label"]
        def_path, def_line, _col = it["anchor"]
        print("\n--- Class Method: %s ---" % label, file=out)
        print("# %s  (def %s:%d-%d)"
              % (label, display_path(def_path, base), def_line + 1, it["end"] + 1), file=out)
        print("# %s  (decl %s:%d)"
              % (label, display_path(it["decl_path"], base), it["decl_line"] + 1), file=out)
        print_refs_and_callers(it, label, mode, base, out)
        print("", file=out)


# ========================= macro queries =========================
def run_macro_query(client, mode, name, wait, include_decl=False, verbose=False, out=None):
    """Declaration site of a macro, plus whatever references clangd has."""
    base = client_root(client)
    out = out or sys.stdout

    try:
        macros = client.resolve_macro(name, deadline_s=wait)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return

    if not macros:
        print("  no macro found matching %r (no '#define %s' under the repo "
              "root, and clangd's index has none either -- check the spelling "
              "or the --root)" % (name, name), file=out)
        return

    if not exact_matches(macros, name):
        print(inexact_match_note(macros, name), file=out)

    if len(macros) > 1 and verbose:
        sys.stderr.write("note: %d macro matches for %r\n" % (len(macros), name))
        for m in macros:
            sys.stderr.write("  %s\n" % item_str(m, base))

    # Fire every macro's references request before awaiting any -- clangd
    # serves read-only queries concurrently, so N matches cost ~1 round-trip
    # instead of N. Same two-phase shape as fetch_refs_and_callers.
    items = []
    for macro_sym in macros:
        loc = macro_sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        start = (loc.get("range") or {}).get("start") or {}
        line = start.get("line") or 0
        col = start.get("character") or 0
        # the symbol's OWN name -- a fuzzy hit would otherwise claim a macro
        # exists under the name that was asked for
        it = {"path": path, "line": line,
              "found_name": macro_sym.get("name") or name,
              "detail": macro_sym.get("detail", "")}
        try:
            it["refs_handle"] = client.references_async(path, line, col, include_decl)
        except Exception as e:
            it["refs_error"] = e
        items.append(it)

    for it in items:
        if "refs_handle" in it:
            try:
                it["refs"] = client.wait_result(it.pop("refs_handle")) or []
            except Exception as e:
                it["refs_error"] = e

    for it in items:
        path, line, found_name = it["path"], it["line"], it["found_name"]
        print("# %s (macro decl %s:%d)"
              % (found_name, display_path(path, base), line + 1), file=out)

        if it["detail"]:
            print("#   detail: %s" % it["detail"], file=out)

        if "refs_error" in it:
            print("  references: query failed (%s)" % it["refs_error"], file=out)
            print("", file=out)
            continue

        refs = it["refs"]
        # clangd doesn't index macro EXPANSION sites, so this returns the
        # #define and little else. A bare count reads as "unused".
        if not _expansion_sites(refs, path, line):
            print("#   note: clangd does not index macro expansion sites, so "
                  "only the definition is listed below -- this is NOT proof "
                  "the macro is unused; grep for %s to find its uses."
                  % found_name, file=out)

        if mode in WANTS_REFS:
            print("%d references of %s" % (len(refs), found_name), file=out)
            for r in refs:
                print("  " + loc_str(r, base), file=out)
        if mode in WANTS_CALLERS:
            print("%d usages (expansions) of %s" % (len(refs), found_name), file=out)
            for r in refs:
                print("  " + loc_str(r, base), file=out)

        print("", file=out)


def _expansion_sites(refs, decl_path, decl_line):
    """References that are not the #define line itself."""
    out = []
    for r in refs:
        same_file = uri_to_path(r.get("uri", "")) == decl_path
        same_line = ((r.get("range") or {}).get("start") or {}).get("line") == decl_line
        if not (same_file and same_line):
            out.append(r)
    return out


# ========================= struct queries =========================
def run_struct_query(client, name, wait, verbose=False, out=None):
    """Declaration site of a struct (or typedef struct)."""
    base = client_root(client)
    out = out or sys.stdout

    try:
        structs = client.resolve_struct(name, deadline_s=wait)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return

    if not structs:
        print("  no struct found matching %r (no struct or class by that "
              "name in clangd's index -- check the spelling or the --root)"
              % name, file=out)
        return

    if not exact_matches(structs, name):
        print(inexact_match_note(structs, name), file=out)

    if len(structs) > 1 and verbose:
        sys.stderr.write("note: %d struct matches for %r\n" % (len(structs), name))
        for s in structs:
            sys.stderr.write("  %s\n" % item_str(s, base))

    for struct_sym in structs:
        loc = struct_sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        line = (((loc.get("range") or {}).get("start") or {}).get("line")) or 0

        found_name = struct_sym.get("name") or name
        print("# %s (struct decl %s:%d)"
              % (found_name, display_path(path, base), line + 1), file=out)

        detail = struct_sym.get("detail", "")
        if detail:
            print("#   detail: %s" % detail, file=out)
        print("", file=out)


# ========================= enum queries =========================
def run_enum_query(client, name, wait, verbose=False, out=None):
    """Declaration site of an enum, plus its members."""
    base = client_root(client)
    out = out or sys.stdout

    try:
        enums = client.resolve_enum(name, deadline_s=wait)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return

    if not enums:
        print("  no enum found matching %r (no enum by that name in clangd's "
              "index -- check the spelling or the --root)" % name, file=out)
        return

    if not exact_matches(enums, name):
        print(inexact_match_note(enums, name), file=out)

    if len(enums) > 1 and verbose:
        sys.stderr.write("note: %d enum matches for %r\n" % (len(enums), name))
        for s in enums:
            sys.stderr.write("  %s\n" % item_str(s, base))

    for enum_sym in enums:
        loc = enum_sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        line = (((loc.get("range") or {}).get("start") or {}).get("line")) or 0
        found_name = enum_sym.get("name") or name

        print("# %s (enum decl %s:%d)"
              % (found_name, display_path(path, base), line + 1), file=out)
        detail = enum_sym.get("detail", "")
        if detail:
            print("#   detail: %s" % detail, file=out)

        try:
            _end_line, members = client.get_enum_end_and_members(path, line)
        except Exception as e:
            print("#   members: query failed (%s)" % e, file=out)
            print("", file=out)
            continue

        if not members:
            print("#   no members found (documentSymbol returned none for this enum)", file=out)
        else:
            print("  %d member%s (in declaration order; clangd's documentSymbol does "
                  "not expose explicit values here -- hover on a member for its value):"
                  % (len(members), "" if len(members) == 1 else "s"), file=out)
            for m in members:
                print("    %s" % m["name"], file=out)
        print("", file=out)


# ========================= header/source switching =========================
def run_switch_header_query(client, path, wait=10, verbose=False, out=None):
    """The counterpart header/source file for `path` (clangd extension)."""
    base = client_root(client)
    out = out or sys.stdout
    path = resolve_input_path(client, path)

    try:
        counterpart = client.switch_source_header(path, timeout=wait)
    except Exception as e:
        print("  switch-header query failed (%s)" % e, file=out)
        return

    where = display_path(path, base)
    if not counterpart:
        print("  no counterpart file for %s (clangd has none -- the file may "
              "be header-only, generated, or outside compile_commands.json)"
              % where, file=out)
        return

    print("# %s  <->  %s" % (where, display_path(counterpart, base)), file=out)


# ========================= include graph =========================
def run_includes_query(client, path, wait=10, verbose=False, out=None):
    """What `path` includes, and what includes `path`.

    Both directions are a text scan, not a clangd query (see
    ClangdClient.list_includes/find_includers) -- best-effort, does not
    evaluate #ifdef, and a shared basename in two directories is ambiguous.
    """
    base = client_root(client)
    out = out or sys.stdout
    path = resolve_input_path(client, path)
    where = display_path(path, base)

    if not os.path.exists(path):
        print("  %s does not exist" % where, file=out)
        return

    try:
        includes = client.list_includes(path)
    except Exception as e:
        print("  could not read %s (%s)" % (where, e), file=out)
        return

    print("# Includes of %s (%d)" % (where, len(includes)), file=out)
    for spelling, is_system in includes:
        print("  %s%s%s" % ("<" if is_system else '"', spelling,
                             ">" if is_system else '"'), file=out)
    print("", file=out)

    includers = client.find_includers(path)
    print("# Included by %s (%d)  [text scan -- see note below]" % (where, len(includers)), file=out)
    for includer_path, lineno in includers:
        print("  %s:%d" % (display_path(includer_path, base), lineno), file=out)
    print("", file=out)
    print("# note: both directions are a text scan of #include lines, not clangd's "
          "index -- #ifdef'd-out includes are not evaluated, and two files sharing "
          "a basename in different directories are indistinguishable here.", file=out)


# ========================= symbol search =========================
# Kind numbers as clangd actually reports them, checked against the fixture
# corpus rather than read off the LSP spec: a C++ struct (plain or typedef)
# comes back as Class, and a #define as String -- LSP has no Macro kind, so
# clangd reuses String. The rest follow the spec. An unrecognised number is
# rendered as-is, so a future clangd that adds kinds degrades to a readable
# label instead of mislabelling the symbol.
SYMBOL_KIND_NAMES = {
    3: "namespace", 5: "class", 6: "method", 8: "field",
    9: "constructor", 10: "enum", 11: "interface", 12: "function",
    13: "variable", 14: "constant", 15: "macro", 22: "enum-member",
    23: "struct", 25: "operator", 26: "type-param",
}

# What a caller may pass as `kind`. The sets are deliberately generous: a
# filter that silently matches nothing reads as "no such symbol exists",
# which is the worst answer a discovery tool can give. 'struct' therefore
# includes Class, because that is where clangd files structs.
SEARCH_KIND_FILTERS = {
    "function":  (12, 6, 9, 25),
    "method":    (6, 9),
    "class":     (5, 23, 11),
    "struct":    (5, 23, 11),
    "enum":      (10, 22),
    "variable":  (13, 14, 8),
    "field":     (8,),
    "namespace": (3,),
    "macro":     (15,),
    "type":      (5, 23, 11, 10, 26),
}

# Enough rows to survey a name across a large repo, few enough not to bury
# the caller. Raise per call with `limit`.
DEFAULT_SEARCH_LIMIT = 30
MAX_SEARCH_LIMIT = 200

# clangd's own --limit-results default. Hitting it exactly almost always
# means the list was cut server-side, and a caller reading a truncated list
# as complete is how "that symbol doesn't exist" gets said with confidence.
CLANGD_RESULT_CAP = 100


def kind_label(kind):
    return SYMBOL_KIND_NAMES.get(kind, "kind%s" % (kind,))


def qualified_name(sym):
    """'Backend::process' from clangd's split name/containerName.

    Search exists to hand names to the other queries, and those resolve
    'Class::method' (see exact_matches) but not a bare 'process', which would
    land on whichever overload in whichever class clangd ranked first.
    """
    name = sym.get("name") or "?"
    container = (sym.get("containerName") or "").strip()
    if not container or name.startswith(container + "::"):
        return name
    return "%s::%s" % (container, name)


def _search_row(sym, base):
    return "  %-11s %-38s %s" % (
        kind_label(sym.get("kind")), qualified_name(sym),
        loc_str(sym.get("location") or {}, base))


def _search_macro_fallback(client, query, wait):
    """Macros that workspace/symbol cannot see, found the way resolve_macro
    finds them.

    clangd only reports a #define once its defining file is open, so a plain
    index lookup misses every macro in the repo and search would blame the
    caller's spelling for it. resolve_macro locates the #define, opens that
    file and re-asks; it returns [] cheaply when nothing defines the name, so
    this is affordable on the miss path only.

    Exact names only -- the locator greps for '#define <name>' -- which is
    the case that matters: a caller who read the macro in some source file
    and wants to know where it lives.
    """
    resolve = getattr(client, "resolve_macro", None)
    if resolve is None:
        return []
    try:
        return resolve(query, deadline_s=wait) or []
    except Exception:
        # A best-effort fallback must not turn a clean "not found" into an
        # error; the caller still gets the no-match message below.
        return []


def run_search_query(client, query, kind=None, limit=DEFAULT_SEARCH_LIMIT,
                     wait=10, verbose=False, out=None):
    """Fuzzy symbol search: the entry point for a caller who does not yet know
    an exact name.

    Inverts this module's usual stance on inexact hits. Everywhere else a
    fuzzy match is a warning sign and gets flagged as one (see
    inexact_match_note); here it is the entire point, so fuzzy results are
    sectioned and labelled instead. The caller picks a qualified name off the
    list and feeds it to run_query / run_class_query.
    """
    base = client_root(client)
    out = out or sys.stdout

    if not isinstance(query, str) or not query.strip():
        print("# search: a non-empty query string is required", file=out)
        return

    kinds = None
    if kind is not None:
        kinds = SEARCH_KIND_FILTERS.get(kind)
        if kinds is None:
            print("# search: unknown kind %r (valid: %s)"
                  % (kind, ", ".join(sorted(SEARCH_KIND_FILTERS))), file=out)
            return

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_SEARCH_LIMIT
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))

    symbols = client.resolve_symbol(query, deadline_s=wait) or []
    if not symbols:
        symbols = _search_macro_fallback(client, query, wait)
    if not symbols:
        print("# no symbols matching %r (clangd's index has nothing close to "
              "that name -- check the spelling or the --root)" % query, file=out)
        return

    matched = symbols
    if kinds is not None:
        matched = [s for s in symbols if s.get("kind") in kinds]
        if not matched and kind == "macro":
            # Asked for macros and found none, but a plain workspace/symbol
            # never sees a macro anyway -- look properly before answering.
            matched = _search_macro_fallback(client, query, wait)
        if not matched:
            # Distinct from "nothing matches": the name exists, the filter is
            # what excluded it. Naming the kinds actually found lets the
            # caller correct the filter instead of concluding it is absent.
            found = ", ".join(sorted({kind_label(s.get("kind")) for s in symbols}))
            print("# %d symbol%s match %r, but none of kind %r (found: %s)"
                  % (len(symbols), "" if len(symbols) == 1 else "s",
                     query, kind, found), file=out)
            return

    exact = exact_matches(matched, query)
    exact_ids = {id(s) for s in exact}
    fuzzy = [s for s in matched if id(s) not in exact_ids]
    exact.sort(key=qualified_name)
    fuzzy.sort(key=qualified_name)

    # Exact hits spend the budget first -- a truncated list that dropped the
    # one symbol the caller literally named would be actively misleading.
    shown_exact = exact[:limit]
    shown_fuzzy = fuzzy[:max(0, limit - len(shown_exact))]
    total = len(matched)
    shown = len(shown_exact) + len(shown_fuzzy)

    print("# %d symbol%s matching %r (%s), showing %d"
          % (total, "" if total == 1 else "s", query,
             "kind=%s" % kind if kind else "any kind", shown),
          file=out)

    if shown_exact:
        print("# exact (%d):" % len(exact), file=out)
        for sym in shown_exact:
            print(_search_row(sym, base), file=out)

    if shown_fuzzy:
        print("# fuzzy -- %s named %r exactly (%d):"
              % ("nothing else is" if shown_exact else "nothing is",
                 query, len(fuzzy)), file=out)
        for sym in shown_fuzzy:
            print(_search_row(sym, base), file=out)

    if shown < total:
        print("# %d more not shown (limit=%d) -- narrow the query or raise limit"
              % (total - shown, limit), file=out)

    if len(symbols) >= CLANGD_RESULT_CAP:
        print("# note: clangd returned its maximum of %d results, so this list "
              "is not exhaustive" % len(symbols), file=out)


# ========================= file outline =========================
# An outline is meant to be the whole file, so this is a safety valve for a
# generated or amalgamated source, not a normal working limit.
DEFAULT_OUTLINE_LIMIT = 300
MAX_OUTLINE_LIMIT = 2000


def _symbol_span(sym):
    """(start, end) 0-based lines for either DocumentSymbol shape.

    Hierarchical replies carry `range` directly; flat SymbolInformation
    nests it under `location`. clangd sends the first, but the reply shape
    is negotiated at startup, so handling only that would break on a server
    that declines the hierarchical capability.
    """
    span = sym.get("range") or (sym.get("location") or {}).get("range") or {}
    start = ((span.get("start") or {}).get("line")) or 0
    end = ((span.get("end") or {}).get("line"))
    return start, (start if end is None else end)


def _outline_rows(nodes, base, depth=0, rows=None):
    """Flatten a DocumentSymbol tree into printable rows, depth first."""
    if rows is None:
        rows = []
    for sym in nodes or []:
        if not isinstance(sym, dict):
            continue
        start, end = _symbol_span(sym)
        label = kind_label(sym.get("kind"))
        # clangd sets detail="class" on a class, which just repeats the kind
        # column; a signature is worth a column, a synonym is not.
        detail = (sym.get("detail") or "").strip()
        if detail == label:
            detail = ""
        rows.append("%s%-11s %-34s %d-%d%s" % (
            "  " + "  " * depth,
            label,
            sym.get("name") or "(unnamed)",
            start + 1, end + 1,
            "   %s" % detail if detail else ""))
        _outline_rows(sym.get("children") or [], base, depth + 1, rows)
    return rows


def run_outline_query(client, path, limit=DEFAULT_OUTLINE_LIMIT, wait=10,
                      verbose=False, out=None):
    """Every symbol declared in one file, nested, with line spans.

    Lets a caller orient in a large file for a few dozen lines of output
    instead of reading the whole thing, and the spans give exact ranges to
    read afterwards. clangd puts the signature in `detail`, so the rows
    carry types without a second query.
    """
    base = client_root(client)
    out = out or sys.stdout
    path = resolve_input_path(client, path)

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_OUTLINE_LIMIT
    limit = max(1, min(limit, MAX_OUTLINE_LIMIT))

    try:
        symbols = client.document_symbol(path, timeout=wait)
    except Exception as e:
        print("# outline of %s failed (%s)" % (display_path(path, base), e), file=out)
        return

    where = display_path(path, base)
    if not symbols:
        # Distinguishable from a parse failure above: clangd answered, the
        # file simply declares nothing at file scope.
        print("# %s declares no symbols (clangd returned an empty outline -- "
              "the file may be header-guard-only, or excluded from "
              "compile_commands.json)" % where, file=out)
        return

    rows = _outline_rows(symbols, base)
    print("# Outline of %s (%d symbol%s)"
          % (where, len(rows), "" if len(rows) == 1 else "s"), file=out)
    for row in rows[:limit]:
        print(row, file=out)
    if len(rows) > limit:
        print("# %d more not shown (limit=%d)" % (len(rows) - limit, limit), file=out)


# ========================= diagnostics =========================
# LSP DiagnosticSeverity.
DIAGNOSTIC_SEVERITIES = {1: "error", 2: "warning", 3: "info", 4: "hint"}

# What a caller may ask for. 'error' alone is the common case for an edit
# loop; the rest are noise until the file compiles.
SEVERITY_FILTERS = {
    "error": (1,),
    "warning": (1, 2),
    "all": (1, 2, 3, 4),
}
DEFAULT_SEVERITY = "all"


def _diagnostic_row(diag, base, path):
    span = diag.get("range") or {}
    start = span.get("start") or {}
    line = (start.get("line") or 0) + 1
    col = (start.get("character") or 0) + 1
    severity = DIAGNOSTIC_SEVERITIES.get(diag.get("severity"), "diagnostic")
    message = " ".join((diag.get("message") or "").split())
    code = diag.get("code")
    bits = [str(b) for b in (diag.get("source"), code) if b not in (None, "")]
    tag = " [%s]" % ":".join(bits) if bits else ""
    return "  %-8s %s:%d:%d  %s%s" % (
        severity, display_path(path, base), line, col, message, tag)


def run_diagnostics_query(client, path, severity=DEFAULT_SEVERITY, wait=15,
                          verbose=False, out=None):
    """Compiler diagnostics for one file, straight from clangd's parse.

    The point is an edit loop that does not need the build system: change a
    file, ask what broke, fix it. clangd reparses on didChange, so the answer
    reflects what is on disk now rather than at first open.
    """
    base = client_root(client)
    out = out or sys.stdout
    path = resolve_input_path(client, path)

    keep = SEVERITY_FILTERS.get(severity)
    if keep is None:
        print("# diagnostics: unknown severity %r (valid: %s)"
              % (severity, ", ".join(sorted(SEVERITY_FILTERS))), file=out)
        return

    try:
        diags, received = client.diagnostics(path, timeout=wait)
    except Exception as e:
        print("# diagnostics for %s failed (%s)"
              % (display_path(path, base), e), file=out)
        return

    where = display_path(path, base)
    if not received:
        # NOT "no problems": clangd never answered, so this says nothing
        # about whether the file is clean, and a caller acting on a false
        # all-clear is the specific failure this branch exists to prevent.
        print("# no diagnostics reported for %s within %ss -- clangd may still "
              "be parsing it, or the file is not in compile_commands.json. "
              "This is NOT a clean bill of health; retry or widen the wait."
              % (where, wait), file=out)
        return

    kept = [d for d in diags if isinstance(d, dict) and d.get("severity") in keep]
    if not diags:
        print("# %s: no diagnostics -- clangd parsed it cleanly" % where, file=out)
        return
    if not kept:
        counts = _severity_counts(diags)
        print("# %s: no diagnostics at severity %r (file has %s)"
              % (where, severity, counts), file=out)
        return

    kept.sort(key=lambda d: (((d.get("range") or {}).get("start") or {}).get("line") or 0,
                             ((d.get("range") or {}).get("start") or {}).get("character") or 0))
    print("# Diagnostics for %s: %s" % (where, _severity_counts(kept)), file=out)
    for diag in kept:
        print(_diagnostic_row(diag, base, path), file=out)


def _severity_counts(diags):
    """'2 errors, 1 warning' -- a summary line the caller can act on without
    reading every row."""
    counts = {}
    for diag in diags:
        if isinstance(diag, dict):
            name = DIAGNOSTIC_SEVERITIES.get(diag.get("severity"), "diagnostic")
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return "none"
    order = ["error", "warning", "info", "hint", "diagnostic"]
    parts = []
    for name in order:
        n = counts.get(name)
        if n:
            parts.append("%d %s%s" % (n, name, "" if n == 1 else "s"))
    return ", ".join(parts)


# ========================= hover queries =========================
def _hover_text(contents):
    """Flatten hover's payload -- MarkupContent, a MarkedString, or an
    array of either -- into displayable text."""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        value = contents.get("value")
        if value:
            language = contents.get("language")
            return "```%s\n%s\n```" % (language, value) if language else value
        return json.dumps(contents, indent=2)
    if isinstance(contents, list):
        parts = []
        for item in contents:
            if isinstance(item, dict):
                value = item.get("value", "")
                language = item.get("language")
                parts.append("```%s\n%s\n```" % (language, value) if language else value)
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(contents)


def resolve_input_path(client, path):
    """Caller-supplied path -> one that can actually be opened.

    Queries REPORT root-relative paths, so callers feed them back in.
    Resolving against the root (not the cwd) is what makes that round trip work.
    """
    if not path or os.path.isabs(path):
        return path
    root = client_root(client)
    if not root:
        return path
    rooted = os.path.join(root, path)
    return rooted if os.path.exists(rooted) else path


def run_hover_query(client, path, line, col, wait=10, verbose=False, out=None):
    """Type, signature and documentation for the symbol at a position."""
    base = client_root(client)
    out = out or sys.stdout
    path = resolve_input_path(client, path)

    try:
        hover = client.hover(path, line, col, timeout=wait)
    except Exception as e:
        print("  hover query failed (%s)" % e, file=out)
        return

    where = display_path(path, base)
    if not hover:
        print("  no hover information available at %s:%d" % (where, line + 1), file=out)
        return

    print("# Hover at %s:%d" % (where, line + 1), file=out)

    span = hover.get("range")
    if span:
        start = (span.get("start") or {}).get("line") or 0
        end = (span.get("end") or {}).get("line") or 0
        print("# Symbol range: line %d-%d" % (start + 1, end + 1), file=out)

    print("# Content:\n%s" % _hover_text(hover.get("contents", "")), file=out)


# ========================= rename (preview only) =========================
# A rename anchored on the wrong symbol is a materially worse failure than a
# read query guessing wrong -- every other query here falls back to a fuzzy
# or first-candidate match, this one refuses instead of guessing.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")


def _rename_edits_by_file(edit):
    """WorkspaceEdit -> {path: [TextEdit, ...]}. clangd may return either
    `changes` or `documentChanges`; only one is populated per reply."""
    out = {}
    changes = edit.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            out[uri_to_path(uri)] = list(edits or [])
        return out
    for dc in edit.get("documentChanges") or []:
        td = dc.get("textDocument") or {}
        uri = td.get("uri")
        if uri:
            out[uri_to_path(uri)] = list(dc.get("edits") or [])
    return out


def run_rename_query(client, name, new_name, wait, verbose=False, out=None):
    """Preview a workspace-wide rename: every edit site clangd computed.
    Never writes to disk -- apply the listed edits yourself."""
    base = client_root(client)
    out = out or sys.stdout

    if not isinstance(new_name, str) or not _IDENTIFIER_RE.match(new_name):
        print("  %r is not a valid C++ identifier for --new-name" % (new_name,), file=out)
        return

    try:
        symbols = client.resolve_symbol(name, deadline_s=wait)
    except Exception as e:
        print("  resolve failed for %r (%s)" % (name, e), file=out)
        return
    if not symbols:
        print("  no symbol matching %r (raise --wait, or check --ccdir)" % name, file=out)
        return

    exact = exact_matches(symbols, name)
    if not exact:
        print("  no EXACT match for %r -- rename refuses to guess from a fuzzy "
              "match (closest: %s)"
              % (name, ", ".join(sorted({s.get("name", "?") for s in symbols[:5]}))), file=out)
        return
    if len(exact) > 1:
        print("  %r is ambiguous (%d exact matches) -- rename refuses to pick one; "
              "qualify it (e.g. Class::%s):" % (name, len(exact), name), file=out)
        for s in exact:
            print("    " + item_str(s, base), file=out)
        return

    sym = exact[0]
    if sym.get("kind") == 15:  # macro (see resolve_macro's docstring on kind 15)
        print("  %r is a macro -- clangd's rename is AST-based and does not "
              "follow macro expansions; edit the #define directly instead" % name, file=out)
        return

    loc = sym.get("location") or {}
    path = uri_to_path(loc.get("uri", ""))
    start = (loc.get("range") or {}).get("start") or {}
    line, col = start.get("line") or 0, start.get("character") or 0

    try:
        edit = client.wait_result(client.rename_async(path, line, col, new_name), timeout=wait) or {}
    except Exception as e:
        print("  rename query failed (%s)" % e, file=out)
        return

    by_file = _rename_edits_by_file(edit)
    total = sum(len(v) for v in by_file.values())
    if not total:
        print("  clangd returned no edits for renaming %r -- nothing to preview" % name, file=out)
        return

    print("# PREVIEW ONLY -- nothing written to disk; apply these edits yourself", file=out)
    print("# renaming %r -> %r: %d edit%s across %d file%s"
          % (name, new_name, total, "" if total == 1 else "s",
             len(by_file), "" if len(by_file) == 1 else "s"), file=out)
    for fpath in sorted(by_file):
        edits = sorted(by_file[fpath],
                       key=lambda e: (((e.get("range") or {}).get("start") or {}).get("line") or 0,
                                      ((e.get("range") or {}).get("start") or {}).get("character") or 0))
        print("\n%s (%d edit%s):" % (display_path(fpath, base), len(edits), "" if len(edits) == 1 else "s"), file=out)
        for e in edits:
            s = (e.get("range") or {}).get("start") or {}
            print("  %d:%d  %s -> %s" % ((s.get("line") or 0) + 1, (s.get("character") or 0) + 1,
                                          name, new_name), file=out)


# ==================== string wrappers (used by the MCP server) ===========
def _as_string(fn, *args, **kwargs):
    buf = io.StringIO()
    kwargs["out"] = buf
    fn(*args, **kwargs)
    return buf.getvalue()


def run_query_as_string(client, mode, name, wait, include_decl=False, verbose=False):
    return _as_string(run_query, client, mode, name, wait, include_decl, verbose)


def run_class_query_as_string(client, mode, name, wait, include_decl=False, verbose=False):
    return _as_string(run_class_query, client, mode, name, wait, include_decl, verbose)


def run_macro_query_as_string(client, mode, name, wait, include_decl=False, verbose=False):
    return _as_string(run_macro_query, client, mode, name, wait, include_decl, verbose)


def run_struct_query_as_string(client, name, wait, verbose=False):
    return _as_string(run_struct_query, client, name, wait, verbose)


def run_hover_query_as_string(client, path, line, col, wait=10, verbose=False):
    return _as_string(run_hover_query, client, path, line, col, wait, verbose)


def run_incoming_calls_query_as_string(client, name, wait=120, verbose=False):
    return _as_string(run_incoming_calls_query, client, name, wait, verbose)


def run_implementations_query_as_string(client, name, wait=120, verbose=False):
    return _as_string(run_implementations_query, client, name, wait, verbose)


def run_enum_query_as_string(client, name, wait, verbose=False):
    return _as_string(run_enum_query, client, name, wait, verbose)


def run_switch_header_query_as_string(client, path, wait=10, verbose=False):
    return _as_string(run_switch_header_query, client, path, wait, verbose)


def run_includes_query_as_string(client, path, wait=10, verbose=False):
    return _as_string(run_includes_query, client, path, wait, verbose)


def run_rename_query_as_string(client, name, new_name, wait=120, verbose=False):
    return _as_string(run_rename_query, client, name, new_name, wait, verbose)


def run_search_query_as_string(client, query, kind=None,
                               limit=DEFAULT_SEARCH_LIMIT, wait=10,
                               verbose=False):
    return _as_string(run_search_query, client, query, kind, limit, wait, verbose)


def run_outline_query_as_string(client, path, limit=DEFAULT_OUTLINE_LIMIT,
                                wait=10, verbose=False):
    return _as_string(run_outline_query, client, path, limit, wait, verbose)


def run_diagnostics_query_as_string(client, path, severity=DEFAULT_SEVERITY,
                                    wait=15, verbose=False):
    return _as_string(run_diagnostics_query, client, path, severity, wait, verbose)

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

# ============ query kinds (shared by the CLI and the daemon) ============
# One table, so the in-process and daemon paths can't disagree about what
# --class/--macro mean. They previously did: the protocol carried only
# is_class, so --macro was ignored unless --no-daemon was passed.
QUERY_KINDS = {
    "function": run_query,
    "class": run_class_query,
    "macro": run_macro_query,
}


def symbol_kind(args):
    """Which QUERY_KINDS entry the CLI flags select."""
    if args.is_class:
        return "class"
    if args.is_macro:
        return "macro"
    return "function"


def client_from_args(args):
    """Build (but do not start) a client configured from the CLI flags."""
    return ClangdClient(args.root, compile_commands_dir=args.ccdir,
                        clangd=args.clangd, request_timeout=args.req_timeout,
                        log=args.verbose)


# =========== CLI tools (the non-refs/callers/all subcommands) ===========
# struct/search/outline/diagnostics/hover/incoming each map onto exactly one
# *_as_string engine function -- the same one clangq_mcp.py wraps as an MCP
# tool. One table drives the subparser, the in-process call, and the daemon
# call, so the CLI and the MCP server can't drift apart on which queries
# exist (see QUERY_KINDS above for the same idea applied to refs/callers/all).
class CliTool:
    def __init__(self, name, help, configure, args_to_kwargs, as_string_fn):
        self.name = name
        self.help = help
        self.configure = configure            # (subparser) -> None
        self.args_to_kwargs = args_to_kwargs  # (Namespace) -> dict for as_string_fn
        self.as_string_fn = as_string_fn      # (client, **kwargs) -> str


def _configure_struct(p):
    p.add_argument("name", help="struct name")


def _configure_search(p):
    p.add_argument("query", help="name or fragment to search for")
    p.add_argument("--kind", default=None, choices=sorted(SEARCH_KIND_FILTERS),
                   help="restrict results to one kind of symbol")
    p.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT,
                   help="max rows to return (default %d, cap %d)"
                        % (DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT))


def _configure_outline(p):
    p.add_argument("path", help="file path, repo-relative or absolute")
    p.add_argument("--limit", type=int, default=DEFAULT_OUTLINE_LIMIT,
                   help="max rows to return (default %d, cap %d)"
                        % (DEFAULT_OUTLINE_LIMIT, MAX_OUTLINE_LIMIT))


def _configure_diagnostics(p):
    p.add_argument("path", help="file path, repo-relative or absolute")
    p.add_argument("--severity", default=DEFAULT_SEVERITY,
                   choices=sorted(SEVERITY_FILTERS),
                   help="lowest severity to report (default: %s)" % DEFAULT_SEVERITY)


def _configure_hover(p):
    p.add_argument("path", help="file path, repo-relative or absolute")
    p.add_argument("line", type=int, help="0-based line number")
    p.add_argument("col", type=int, help="0-based column number")


def _configure_incoming(p):
    p.add_argument("name", help="function name")


def _configure_implementations(p):
    p.add_argument("name", help="virtual method/function name")


def _configure_enum(p):
    p.add_argument("name", help="enum name")


def _configure_switch_header(p):
    p.add_argument("path", help="file path, repo-relative or absolute")


def _configure_includes(p):
    p.add_argument("path", help="file path, repo-relative or absolute")


def _configure_rename(p):
    p.add_argument("name", help="existing symbol name (must be exact and unambiguous)")
    p.add_argument("new_name", help="replacement identifier")


CLI_TOOLS = {
    "struct": CliTool(
        "struct", "declaration of a struct (or typedef struct)",
        _configure_struct,
        lambda a: {"name": a.name, "wait": a.wait, "verbose": a.verbose},
        run_struct_query_as_string),
    "search": CliTool(
        "search", "fuzzy symbol search by name or fragment",
        _configure_search,
        lambda a: {"query": a.query, "kind": a.kind, "limit": a.limit,
                   "wait": a.wait, "verbose": a.verbose},
        run_search_query_as_string),
    "outline": CliTool(
        "outline", "every symbol declared in one file, nested, with line spans",
        _configure_outline,
        lambda a: {"path": a.path, "limit": a.limit, "wait": a.wait,
                   "verbose": a.verbose},
        run_outline_query_as_string),
    "diagnostics": CliTool(
        "diagnostics", "clangd's compiler diagnostics (errors/warnings) for one file",
        _configure_diagnostics,
        lambda a: {"path": a.path, "severity": a.severity, "wait": a.wait,
                   "verbose": a.verbose},
        run_diagnostics_query_as_string),
    "hover": CliTool(
        "hover", "type/signature/doc for the symbol at a file position",
        _configure_hover,
        lambda a: {"path": a.path, "line": a.line, "col": a.col,
                   "wait": a.wait, "verbose": a.verbose},
        run_hover_query_as_string),
    "incoming": CliTool(
        "incoming", "every function/method that calls a function (callers only, no def/decl)",
        _configure_incoming,
        lambda a: {"name": a.name, "wait": a.wait, "verbose": a.verbose},
        run_incoming_calls_query_as_string),
    "implementations": CliTool(
        "implementations", "overriding definitions of a virtual method/function",
        _configure_implementations,
        lambda a: {"name": a.name, "wait": a.wait, "verbose": a.verbose},
        run_implementations_query_as_string),
    "enum": CliTool(
        "enum", "declaration and members of an enum",
        _configure_enum,
        lambda a: {"name": a.name, "wait": a.wait, "verbose": a.verbose},
        run_enum_query_as_string),
    "switch-header": CliTool(
        "switch-header", "the counterpart header/source file for a path",
        _configure_switch_header,
        lambda a: {"path": a.path, "wait": a.wait, "verbose": a.verbose},
        run_switch_header_query_as_string),
    "includes": CliTool(
        "includes", "what a file includes, and what includes it (text scan)",
        _configure_includes,
        lambda a: {"path": a.path, "wait": a.wait, "verbose": a.verbose},
        run_includes_query_as_string),
    "rename": CliTool(
        "rename", "PREVIEW ONLY: every edit site a workspace rename would touch",
        _configure_rename,
        lambda a: {"name": a.name, "new_name": a.new_name, "wait": a.wait,
                   "verbose": a.verbose},
        run_rename_query_as_string),
}


# ===================== daemon server =====================
# Request handlers, one per command. Each takes (client, req, args) and
# returns (response_dict, keep_serving).
DAEMON_COMMANDS = {}


def _daemon_command(name):
    def register(fn):
        DAEMON_COMMANDS[name] = fn
        return fn
    return register


@_daemon_command("ping")
def _cmd_ping(client, req, args):
    return {"ok": True}, True


@_daemon_command("shutdown")
def _cmd_shutdown(client, req, args):
    return {"ok": True}, False


@_daemon_command("query")
def _cmd_query(client, req, args):
    kind = req.get("kind", "function")
    query = QUERY_KINDS.get(kind)
    if query is None:
        return {"error": "unknown query kind %r" % kind}, True

    buf = io.StringIO()
    try:
        query(client, req.get("mode", "all"), req["name"], req.get("wait", 10),
              req.get("include_decl", False), args.verbose, out=buf)
    except Exception as e:
        return {"error": repr(e)}, True
    return {"out": buf.getvalue()}, True


@_daemon_command("tool")
def _cmd_tool(client, req, args):
    """Dispatch one of CLI_TOOLS (struct/search/outline/diagnostics/hover/
    incoming) -- the counterpart to _cmd_query for everything that isn't a
    function/class/macro refs-callers-all query."""
    tool = CLI_TOOLS.get(req.get("tool"))
    if tool is None:
        return {"error": "unknown tool %r" % req.get("tool")}, True

    try:
        out = tool.as_string_fn(client, **(req.get("kwargs") or {}))
    except Exception as e:
        return {"error": repr(e)}, True
    return {"out": out}, True


def serve(args):
    """Run the per-repo daemon: one warm clangd, many concurrent clients.

    Each accepted connection is handed to its own worker thread (see
    _accept_loop) rather than handled one at a time in the accept loop --
    ClangdClient already pipelines concurrent LSP requests (see
    _request_async's docstring, and the _send framing invariant documented
    on ClangdClient._send), so a slow query from one caller must not block
    every other CLI invocation against this daemon for its whole duration.
    """
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

    client = client_from_args(args).start()
    client.prime_index()
    if args.verbose:
        sys.stderr.write("[daemon] clangd warm; ready for queries\n")

    try:
        _accept_loop(srv, client, args, sock_path)
    finally:
        client.shutdown()
        try:
            srv.close()
        except OSError:
            pass
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        if args.verbose:
            sys.stderr.write("[daemon] stopped\n")


def _accept_loop(srv, client, args, sock_path):
    """Accept connections and hand each to its own worker thread, until a
    "shutdown" command sets `stop_event` (see _serve_one/_signal_stop).

    accept() blocks, so a stop_event alone would leave this loop waiting
    forever for one more connection that may never arrive -- _signal_stop
    also opens a throwaway connection to this same socket to wake it.
    """
    stop_event = threading.Event()
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except OSError:
            break  # e.g. the socket was closed out from under us
        if stop_event.is_set():
            # A real client can race the wake connection into the accept
            # backlog. Best-effort courtesy, not a guarantee -- whether
            # this loop notices stop_event here or exits one line up via
            # the while-condition (skipping this entirely) is itself a
            # race. Either way the client must not crash: see
            # _recv_daemon_response, the actual backstop.
            try:
                _send_json(conn, {"error": "daemon is shutting down"})
            except Exception:
                pass
            conn.close()
            break
        threading.Thread(target=_serve_one, args=(conn, client, args, stop_event, sock_path),
                         daemon=True).start()


def _serve_one(conn, client, args, stop_event, sock_path):
    """Answer one already-accepted connection, on its own thread.

    A bad request must not kill the daemon -- it outlives its clients, so
    anything unhandled here strands the warm index.
    """
    try:
        line = _recv_line(conn)
        if not line:
            return
        try:
            req = json.loads(line)
        except ValueError as e:
            _send_json(conn, {"error": "malformed request (%s)" % e})
            return
        if not isinstance(req, dict):
            _send_json(conn, {"error": "request must be a JSON object"})
            return

        handler = DAEMON_COMMANDS.get(req.get("cmd"))
        if handler is None:
            _send_json(conn, {"error": "unknown cmd %r (known: %s)"
                              % (req.get("cmd"), ", ".join(sorted(DAEMON_COMMANDS)))})
            return

        response, keep_serving = handler(client, req, args)
        _send_json(conn, response)
        if not keep_serving:
            _signal_stop(stop_event, sock_path)
    except Exception as e:
        try:
            _send_json(conn, {"error": "daemon error: %r" % e})
        except Exception:
            pass
    finally:
        conn.close()


def _signal_stop(stop_event, sock_path):
    """Set `stop_event` and wake the accept loop out of its blocking
    accept() so it notices promptly instead of waiting for a connection
    that may never come."""
    stop_event.set()
    try:
        wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            wake.connect(sock_path)
        finally:
            wake.close()
    except OSError:
        pass


# ===================== daemon client =====================
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


def _recv_daemon_response(conn):
    """Read and parse one daemon response line.

    A daemon that closes the connection without answering (e.g. a client
    connection racing an explicit `stop` into the accept backlog) leaves
    _recv_line returning "" here, which json.loads("") raises on -- report
    that as cleanly as any other daemon-side error, not a raw traceback.
    """
    try:
        return json.loads(_recv_line(conn))
    except ValueError:
        return {"error": "no valid response from the daemon "
                         "(it may have shut down mid-request; retry)"}


def daemon_query(args):
    sock_path, log_path = _paths(args.root)
    conn = _try_connect(sock_path)
    if conn is None and not args.no_daemon:
        _start_daemon(args, log_path)
        conn = _wait_connect(sock_path, 30)
    if conn is None:
        sys.exit("could not connect to a daemon for %s (log: %s)"
                 % (os.path.abspath(args.root), log_path))

    try:
        _send_json(conn, {
            "cmd": "query",
            "kind": symbol_kind(args),
            "mode": args.command,
            "name": args.name,
            "wait": args.wait,
            "include_decl": args.include_decl,
        })
        resp = _recv_daemon_response(conn)
    finally:
        conn.close()

    if "error" in resp:
        print("  daemon error:", resp["error"])
    else:
        sys.stdout.write(resp.get("out", ""))


def daemon_tool(args, tool):
    """Send one CLI_TOOLS request to the daemon -- the counterpart to
    daemon_query for struct/search/outline/diagnostics/hover/incoming."""
    sock_path, log_path = _paths(args.root)
    conn = _try_connect(sock_path)
    if conn is None and not args.no_daemon:
        _start_daemon(args, log_path)
        conn = _wait_connect(sock_path, 30)
    if conn is None:
        sys.exit("could not connect to a daemon for %s (log: %s)"
                 % (os.path.abspath(args.root), log_path))

    try:
        _send_json(conn, {
            "cmd": "tool",
            "tool": tool.name,
            "kwargs": tool.args_to_kwargs(args),
        })
        resp = _recv_daemon_response(conn)
    finally:
        conn.close()

    if "error" in resp:
        print("  daemon error:", resp["error"])
    else:
        sys.stdout.write(resp.get("out", ""))


def stop(args):
    sock_path, _ = _paths(args.root)
    conn = _try_connect(sock_path)
    if not conn:
        print("no daemon running for %s" % os.path.abspath(args.root))
        return
    try:
        _send_json(conn, {"cmd": "shutdown"})
        _recv_daemon_response(conn)
        print("daemon stopped")
    finally:
        conn.close()


# ===================== interactive shell (single process) ===========
HELP = ("commands:\n"
        "  refs <symbol> [decl]        cross-file references ('decl' includes the declaration)\n"
        "  callers <symbol>            incoming calls\n"
        "  all <symbol>                refs + callers\n"
        "  struct <symbol>             struct (or typedef struct) declaration\n"
        "  incoming <symbol>           incoming callers only, no def/decl header\n"
        "  search <query> [kind]       fuzzy symbol search, optionally restricted to one kind\n"
        "  outline <path> [limit]      every symbol declared in one file\n"
        "  diagnostics <path> [sev]    compiler diagnostics for one file (sev: error/warning/all)\n"
        "  hover <path> <line> <col>   type/signature/doc at a position (0-based)\n"
        "  implementations <symbol>    overriding definitions of a virtual method/function\n"
        "  enum <symbol>               enum declaration and members\n"
        "  switch-header <path>        the counterpart header/source file\n"
        "  includes <path>             what a file includes, and what includes it\n"
        "  rename <symbol> <new_name>  PREVIEW ONLY: every edit site a rename would touch\n"
        "  help                        this message\n"
        "  quit | exit                 shut down clangd and leave")

REPL_QUIT = ("quit", "exit", "q")
REPL_HELP = ("help", "h", "?")


def _repl_symbol_query(client, cmd, parts, wait, verbose):
    """refs / callers / all -- the original REPL commands, function-only."""
    if len(parts) < 2:
        print("  usage: %s <symbol>" % cmd)
        return
    include_decl = (cmd == "refs" and len(parts) > 2 and
                    parts[2].lower() in ("decl", "--include-decl"))
    run_query(client, cmd, parts[1], wait, include_decl, verbose)


def _repl_struct(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: struct <symbol>")
        return
    sys.stdout.write(run_struct_query_as_string(client, parts[1], wait, verbose=verbose))


def _repl_incoming(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: incoming <symbol>")
        return
    sys.stdout.write(run_incoming_calls_query_as_string(client, parts[1], wait, verbose=verbose))


def _repl_search(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: search <query> [kind]")
        return
    kind = parts[2] if len(parts) > 2 else None
    sys.stdout.write(run_search_query_as_string(
        client, parts[1], kind=kind, wait=wait, verbose=verbose))


def _repl_outline(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: outline <path> [limit]")
        return
    limit = DEFAULT_OUTLINE_LIMIT
    if len(parts) > 2:
        try:
            limit = int(parts[2])
        except ValueError:
            print("  limit must be an integer")
            return
    sys.stdout.write(run_outline_query_as_string(
        client, parts[1], limit=limit, wait=wait, verbose=verbose))


def _repl_diagnostics(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: diagnostics <path> [severity]")
        return
    severity = parts[2] if len(parts) > 2 else DEFAULT_SEVERITY
    sys.stdout.write(run_diagnostics_query_as_string(
        client, parts[1], severity=severity, wait=wait, verbose=verbose))


def _repl_hover(client, parts, wait, verbose):
    if len(parts) < 4:
        print("  usage: hover <path> <line> <col>")
        return
    try:
        line, col = int(parts[2]), int(parts[3])
    except ValueError:
        print("  line and col must be integers")
        return
    sys.stdout.write(run_hover_query_as_string(
        client, parts[1], line, col, wait=wait, verbose=verbose))


def _repl_implementations(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: implementations <symbol>")
        return
    sys.stdout.write(run_implementations_query_as_string(client, parts[1], wait, verbose=verbose))


def _repl_enum(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: enum <symbol>")
        return
    sys.stdout.write(run_enum_query_as_string(client, parts[1], wait, verbose=verbose))


def _repl_switch_header(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: switch-header <path>")
        return
    sys.stdout.write(run_switch_header_query_as_string(client, parts[1], wait=wait, verbose=verbose))


def _repl_includes(client, parts, wait, verbose):
    if len(parts) < 2:
        print("  usage: includes <path>")
        return
    sys.stdout.write(run_includes_query_as_string(client, parts[1], wait=wait, verbose=verbose))


def _repl_rename(client, parts, wait, verbose):
    if len(parts) < 3:
        print("  usage: rename <symbol> <new_name>")
        return
    sys.stdout.write(run_rename_query_as_string(client, parts[1], parts[2], wait=wait, verbose=verbose))


# name -> (client, parts, wait, verbose) -> None, matching everything except
# refs/callers/all (handled directly by _repl_symbol_query, since it alone
# takes a `cmd` -- the query mode -- rather than being one fixed query).
REPL_TOOL_COMMANDS = {
    "struct": _repl_struct,
    "incoming": _repl_incoming,
    "search": _repl_search,
    "outline": _repl_outline,
    "diagnostics": _repl_diagnostics,
    "hover": _repl_hover,
    "implementations": _repl_implementations,
    "enum": _repl_enum,
    "switch-header": _repl_switch_header,
    "includes": _repl_includes,
    "rename": _repl_rename,
}


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

        if cmd in REPL_QUIT:
            break
        if cmd in REPL_HELP:
            print(HELP)
            continue

        try:
            if cmd in ("refs", "callers", "all"):
                _repl_symbol_query(client, cmd, parts, wait, verbose)
            elif cmd in REPL_TOOL_COMMANDS:
                REPL_TOOL_COMMANDS[cmd](client, parts, wait, verbose)
            else:
                print("  unknown command: %s (try 'help')" % cmd)
        except Exception as e:
            print("  error: %s" % e)


# ===================== entrypoint =====================
QUERY_MODES = ("refs", "callers", "all")
SESSION_MODES = ("shell", "daemon", "stop")


def _run_shell(args):
    client = client_from_args(args).start()
    try:
        client.prime_index()
        repl(client, args.wait, args.verbose)
    finally:
        client.shutdown()


def _run_query_in_process(args):
    client = client_from_args(args).start()
    try:
        client.prime_index()
        QUERY_KINDS[symbol_kind(args)](
            client, args.command, args.name, args.wait, args.include_decl, args.verbose)
    finally:
        client.shutdown()


def _run_tool_in_process(args, tool):
    """In-process (--no-daemon) counterpart to daemon_tool."""
    client = client_from_args(args).start()
    try:
        client.prime_index()
        sys.stdout.write(tool.as_string_fn(client, **tool.args_to_kwargs(args)))
    finally:
        client.shutdown()


# Modes that take no symbol name.
SESSION_HANDLERS = {
    "daemon": serve,
    "stop": stop,
    "shell": _run_shell,
}


def build_parser():
    ap = argparse.ArgumentParser(
        description="Semantic codebase queries via clangd (standalone, daemon-backed)")

    # Shared by every subcommand. A parent parser (not top-level arguments)
    # because argparse subparsers each need their own copy of --root etc. --
    # top-level optionals aren't visible to a subparser's own argv slice.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=".", help="repo root (default: cwd)")
    common.add_argument("--ccdir", default=None,
                        help="dir with compile_commands.json (default: <root> then <root>/build)")
    common.add_argument("--wait", type=int, default=10,
                        help="seconds to wait for the background index / a single request")
    common.add_argument("--req-timeout", type=int, default=10,
                        help="per-request timeout (raise for heavy TUs)")
    common.add_argument("--clangd", default="clangd",
                        help="clangd binary name/path (e.g. clangd-14)")
    common.add_argument("--no-daemon", action="store_true",
                        help="run the query in-process instead of via the daemon")
    common.add_argument("-v", "--verbose", action="store_true")

    sub = ap.add_subparsers(dest="command", required=True)

    # refs / callers / all -- a function, class, or macro (mutually exclusive).
    for mode in QUERY_MODES:
        p = sub.add_parser(mode, parents=[common],
                           help="%s of a function, class (--class), or macro (--macro)" % mode)
        p.add_argument("name", help="symbol name, plain or qualified (Class::method)")
        kind = p.add_mutually_exclusive_group()
        kind.add_argument("--class", action="store_true", dest="is_class",
                          help="treat name as a class instead of a function")
        kind.add_argument("--macro", action="store_true", dest="is_macro",
                          help="treat name as a macro instead of a function")
        p.add_argument("--include-decl", action="store_true",
                       help="refs mode: include the declaration")

    # struct/search/outline/diagnostics/hover/incoming -- everything else
    # clangq_mcp.py exposes as an MCP tool. See CLI_TOOLS.
    for name, tool in CLI_TOOLS.items():
        p = sub.add_parser(name, parents=[common], help=tool.help)
        tool.configure(p)

    for mode in SESSION_MODES:
        sub.add_parser(mode, parents=[common], help="%s session" % mode)

    return ap


def main():
    # clangd's hover/detail text is UTF-8 and can contain characters (e.g.
    # an arrow in a lambda signature) outside a Windows console's cp1252
    # default -- without this, printing them crashes with
    # UnicodeEncodeError instead of showing the query result.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = build_parser()
    args = ap.parse_args()

    session = SESSION_HANDLERS.get(args.command)
    if session is not None:
        session(args)
        return

    tool = CLI_TOOLS.get(args.command)
    if tool is not None:
        if args.no_daemon:
            _run_tool_in_process(args, tool)
        else:
            daemon_tool(args, tool)
        return

    # Otherwise it's refs/callers/all -- argparse already enforced --class
    # and --macro as mutually exclusive and `name` as required.
    if args.no_daemon:
        _run_query_in_process(args)
    else:
        daemon_query(args)


if __name__ == "__main__":
    main()
