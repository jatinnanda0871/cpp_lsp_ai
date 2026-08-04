#include "util.h"
#include "prelude.h"


Config::Config() : maxItems(CORPUS_MAX_ITEMS), verbose(false), name("default") {}

void Config::applyDefaults() {
    maxItems = CORPUS_MAX_ITEMS;
    verbose = false;
    name = "default";
}

bool Config::isValid() const {
    return maxItems > 0 && maxItems <= CORPUS_MAX_ITEMS;
}

namespace util {

// static free function: internal linkage, only visible in this TU
static int internalHelper(int v) {
    return v * 2;
}

int clampInt(int v, int lo, int hi) {
    return CORPUS_CLAMP(v, lo, hi);
}

double clampDouble(double v, double lo, double hi) {
    return CORPUS_CLAMP(v, lo, hi);
}

corpus::Str trim(const corpus::Str& s) {
    corpus::size_type start = 0;
    corpus::size_type end = s.size();
    while (start < end && corpus::isSpaceChar(s[start])) {
        ++start;
    }
    while (end > start && corpus::isSpaceChar(s[end - 1])) {
        --end;
    }
    return s.substr(start, end - start);
}

corpus::Str toUpper(const corpus::Str& s) {
    corpus::Str out = s;
    for (corpus::size_type i = 0; i < out.size(); ++i) {
        out[i] = corpus::toUpperChar(out[i]);
    }
    return out;
}

bool startsWith(const corpus::Str& s, const corpus::Str& prefix) {
    if (prefix.size() > s.size()) {
        return false;
    }
    return s.hasPrefix(prefix);
}

int parseInt(const corpus::Str& s, bool* ok) {
    int result = 0;
    bool any = false;
    for (corpus::size_type i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (c < '0' || c > '9') {
            if (ok) {
                *ok = false;
            }
            return 0;
        }
        result = result * 10 + (c - '0');
        any = true;
    }
    if (ok) {
        *ok = any;
    }
    return result;
}

int sumRange(int from, int to) {
    int total = 0;
    for (int i = from; i <= to; ++i) {
        total += internalHelper(i);
    }
    return total;
}

void swapInts(int* a, int* b) {
    if (!a || !b) {
        return;
    }
    int t = *a;
    *a = *b;
    *b = t;
}

}  // namespace util
