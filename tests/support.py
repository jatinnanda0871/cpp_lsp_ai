"""Shared test helpers: clangd discovery and test-corpus setup.

Standard library only, matching the rest of the project (README: "no
third-party deps"). Tests use unittest, so `python3 -m unittest` works with
no install step.
"""
import json
import os
import shutil
import subprocess
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
CORPUS_DIR = os.path.join(TESTS_DIR, "fixtures", "corpus")
CORPUS_BUILD_DIR = os.path.join(CORPUS_DIR, "build")

# Make scripts/ importable from tests without installing anything.
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


# ---------------------------------------------------------------- clangd ----
# Production target is clangd-14, so prefer it; fall back to whatever is on
# PATH so the suite still runs (and still finds bugs) on other machines.
CLANGD_CANDIDATES = [
    "clangd-14", "clangd-14.0", "clangd14",
    "clangd-15", "clangd-16", "clangd-17", "clangd-18",
    "clangd",
]


def find_clangd():
    """Return a clangd binary path, preferring clangd-14, or None."""
    override = os.environ.get("CLANGQ_TEST_CLANGD")
    if override:
        return override if (os.path.exists(override) or shutil.which(override)) else None
    for name in CLANGD_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def clangd_version(binary):
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- corpus ----
CORPUS_SOURCES = [
    "src/shapes.cpp", "src/overloads.cpp", "src/containers.cpp",
    "src/namespaced.cpp", "src/util.cpp", "src/chains.cpp",
    "src/big.cpp", "src/main.cpp",
]


def _cmake_available():
    return shutil.which("cmake") is not None


def _build_with_cmake():
    """Preferred path: let CMake emit compile_commands.json."""
    try:
        subprocess.run(
            ["cmake", "-S", CORPUS_DIR, "-B", CORPUS_BUILD_DIR,
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"],
            capture_output=True, text=True, timeout=300, check=True)
    except Exception:
        return False
    return os.path.exists(os.path.join(CORPUS_BUILD_DIR, "compile_commands.json"))


def _write_compile_commands_directly():
    """Fallback when cmake is unavailable.

    clangd only needs the compile flags for each file -- it never runs the
    compiler or links anything -- so a hand-written compilation database
    indexes exactly the same as a CMake-generated one.
    """
    include_dir = os.path.join(CORPUS_DIR, "include")
    entries = []
    for rel in CORPUS_SOURCES:
        src = os.path.join(CORPUS_DIR, rel.replace("/", os.sep))
        entries.append({
            "directory": CORPUS_DIR,
            "file": src,
            "command": "c++ -std=c++14 -I%s -c %s" % (include_dir, src),
        })
    if not os.path.isdir(CORPUS_BUILD_DIR):
        os.makedirs(CORPUS_BUILD_DIR)
    path = os.path.join(CORPUS_BUILD_DIR, "compile_commands.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    return os.path.exists(path)


def ensure_corpus_db(force=False):
    """Make sure the corpus has a compile_commands.json. Returns its dir.

    Uses CMake when available (the realistic path, and what production
    repos do), otherwise writes the database directly.
    """
    db = os.path.join(CORPUS_BUILD_DIR, "compile_commands.json")
    if os.path.exists(db) and not force:
        return CORPUS_BUILD_DIR
    if _cmake_available() and _build_with_cmake():
        return CORPUS_BUILD_DIR
    if _write_compile_commands_directly():
        return CORPUS_BUILD_DIR
    raise RuntimeError("could not produce compile_commands.json for the corpus")


def corpus_source(rel):
    """Absolute path to a corpus file, e.g. corpus_source('src/shapes.cpp')."""
    return os.path.join(CORPUS_DIR, rel.replace("/", os.sep))


def line_of(rel, needle, occurrence=1):
    """0-based line number of the Nth line containing `needle`.

    Lets tests assert against the real fixture instead of hardcoded line
    numbers that break whenever the corpus is edited.
    """
    path = corpus_source(rel)
    seen = 0
    with open(path, "r", encoding="utf-8") as f:
        for i, text in enumerate(f):
            if needle in text:
                seen += 1
                if seen == occurrence:
                    return i
    raise AssertionError("%r not found in %s (occurrence %d)" % (needle, rel, occurrence))
