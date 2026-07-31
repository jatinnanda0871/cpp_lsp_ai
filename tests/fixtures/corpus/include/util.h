// Macros, structs (plain and typedef'd), and free functions.
// clangd indexes #define as kind=15 (Constant) and typedef struct as kind=5
// (Class), which is what resolve_macro / resolve_struct rely on.
#ifndef CORPUS_UTIL_H
#define CORPUS_UTIL_H

#include "prelude.h"


// object-like macro, used in several files
#define CORPUS_MAX_ITEMS 128

// function-like macro, expanded in util.cpp and main.cpp
#define CORPUS_CLAMP(v, lo, hi) ((v) < (lo) ? (lo) : ((v) > (hi) ? (hi) : (v)))

// macro that is DEFINED but never used anywhere -- a zero-reference case
#define CORPUS_UNUSED_MACRO 0

#define CORPUS_LOG_LEVEL 3

// plain struct
struct Point {
    int x;
    int y;
};

// typedef struct -- clangd reports this as kind=5, not kind=13
typedef struct {
    double lat;
    double lon;
} GeoCoord;

// struct with methods
struct Config {
    int maxItems;
    bool verbose;
    corpus::Str name;

    Config();
    void applyDefaults();
    bool isValid() const;
};

// struct that is declared but NEVER referenced anywhere
struct OrphanStruct {
    int unused_field;
};

namespace util {

// free functions, declared here and defined in util.cpp
int clampInt(int v, int lo, int hi);
double clampDouble(double v, double lo, double hi);
corpus::Str trim(const corpus::Str& s);
corpus::Str toUpper(const corpus::Str& s);
bool startsWith(const corpus::Str& s, const corpus::Str& prefix);
int parseInt(const corpus::Str& s, bool* ok);
int sumRange(int from, int to);
void swapInts(int* a, int* b);

// declared but never defined anywhere -- resolves to a declaration with no body
int neverDefined(int x);

// static (internal linkage) helper lives in util.cpp only

}  // namespace util

#endif
