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
        print("# %s  (decl %s:%d)"
              % (label, display_path(it["decl_path"], base), it["decl_line"] + 1), file=out)
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

    for macro_sym in macros:
        loc = macro_sym.get("location") or {}
        path = uri_to_path(loc.get("uri", ""))
        start = (loc.get("range") or {}).get("start") or {}
        line = start.get("line") or 0
        col = start.get("character") or 0

        # the symbol's OWN name -- a fuzzy hit would otherwise claim a macro
        # exists under the name that was asked for
        found_name = macro_sym.get("name") or name
        print("# %s (macro decl %s:%d)"
              % (found_name, display_path(path, base), line + 1), file=out)

        detail = macro_sym.get("detail", "")
        if detail:
            print("#   detail: %s" % detail, file=out)

        try:
            refs = client.references(path, line, col, include_decl)
        except Exception as e:
            print("  references: query failed (%s)" % e, file=out)
            print("", file=out)
            continue

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
def run_struct_query(client, name, wait, include_decl=False, verbose=False, out=None):
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
        symbols = client.document_symbol(path)
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
        hover = client.hover(path, line, col)
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


def run_struct_query_as_string(client, name, wait, include_decl=False, verbose=False):
    return _as_string(run_struct_query, client, name, wait, include_decl, verbose)


def run_hover_query_as_string(client, path, line, col, wait=10, verbose=False):
    return _as_string(run_hover_query, client, path, line, col, wait, verbose)


def run_incoming_calls_query_as_string(client, name, wait=120, verbose=False):
    return _as_string(run_incoming_calls_query, client, name, wait, verbose)


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


def serve(args):
    """Run the per-repo daemon: one warm clangd, many short-lived clients."""
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
        while _serve_one(srv, client, args):
            pass
    finally:
        client.shutdown()
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        if args.verbose:
            sys.stderr.write("[daemon] stopped\n")


def _serve_one(srv, client, args):
    """Answer one connection. Returns False to stop serving.

    A bad request must not kill the daemon -- it outlives its clients, so
    anything unhandled here strands the warm index.
    """
    conn, _ = srv.accept()
    try:
        line = _recv_line(conn)
        if not line:
            return True
        try:
            req = json.loads(line)
        except ValueError as e:
            _send_json(conn, {"error": "malformed request (%s)" % e})
            return True
        if not isinstance(req, dict):
            _send_json(conn, {"error": "request must be a JSON object"})
            return True

        handler = DAEMON_COMMANDS.get(req.get("cmd"))
        if handler is None:
            _send_json(conn, {"error": "unknown cmd %r (known: %s)"
                              % (req.get("cmd"), ", ".join(sorted(DAEMON_COMMANDS)))})
            return True

        response, keep_serving = handler(client, req, args)
        _send_json(conn, response)
        return keep_serving
    except Exception as e:
        try:
            _send_json(conn, {"error": "daemon error: %r" % e})
        except Exception:
            pass
        return True
    finally:
        conn.close()


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
            "mode": args.mode,
            "name": args.name,
            "wait": args.wait,
            "include_decl": args.include_decl,
        })
        resp = json.loads(_recv_line(conn))
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
        json.loads(_recv_line(conn))
        print("daemon stopped")
    finally:
        conn.close()


# ===================== interactive shell (single process) ===========
HELP = ("commands:\n"
        "  refs <symbol> [decl]     cross-file references ('decl' includes the declaration)\n"
        "  callers <symbol>         incoming calls\n"
        "  all <symbol>             refs + callers\n"
        "  help                     this message\n"
        "  quit | exit              shut down clangd and leave")

REPL_QUIT = ("quit", "exit", "q")
REPL_HELP = ("help", "h", "?")


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
        if cmd not in ("refs", "callers", "all"):
            print("  unknown command: %s (try 'help')" % cmd)
            continue
        if len(parts) < 2:
            print("  usage: %s <symbol>" % cmd)
            continue

        include_decl = (cmd == "refs" and len(parts) > 2 and
                        parts[2].lower() in ("decl", "--include-decl"))
        try:
            run_query(client, cmd, parts[1], wait, include_decl, verbose)
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
            client, args.mode, args.name, args.wait, args.include_decl, args.verbose)
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
    ap.add_argument("mode", choices=list(QUERY_MODES) + list(SESSION_MODES))
    ap.add_argument("name", nargs="?", help="symbol name (omit for shell/daemon/stop)")
    ap.add_argument("--class", action="store_true", dest="is_class",
                    help="treat name as a class instead of a function")
    ap.add_argument("--macro", action="store_true", dest="is_macro",
                    help="treat name as a macro instead of a function")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--ccdir", default=None,
                    help="dir with compile_commands.json (default: <root> then <root>/build)")
    ap.add_argument("--wait", type=int, default=10,
                    help="seconds to wait for the background index")
    ap.add_argument("--req-timeout", type=int, default=10,
                    help="per-request timeout (raise for heavy TUs)")
    ap.add_argument("--clangd", default="clangd",
                    help="clangd binary name/path (e.g. clangd-14)")
    ap.add_argument("--include-decl", action="store_true",
                    help="refs mode: include the declaration")
    ap.add_argument("--no-daemon", action="store_true",
                    help="run the query in-process instead of via the daemon")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()

    session = SESSION_HANDLERS.get(args.mode)
    if session is not None:
        session(args)
        return

    if not args.name:
        ap.error("a symbol name is required for %r" % args.mode)
    if args.is_class and args.is_macro:
        ap.error("--class and --macro are mutually exclusive")

    if args.no_daemon:
        _run_query_in_process(args)
    else:
        daemon_query(args)


if __name__ == "__main__":
    main()
