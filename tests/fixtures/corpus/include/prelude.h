// Minimal freestanding replacements for the bits of the standard library the
// corpus needs.
//
// The corpus deliberately includes NO system headers. clangd must be able to
// parse every file with nothing but these, so the suite produces identical
// results on any machine -- including one with no C++ toolchain installed.
// If a system header were missing, clangd's AST would be broken and queries
// would silently return zero references/callers, which looks exactly like a
// bug in the query engine.
//
// These are also useful test subjects in their own right: Vec<T> is a class
// template, Str has operator overloads.
#ifndef CORPUS_PRELUDE_H
#define CORPUS_PRELUDE_H

namespace corpus {

typedef unsigned long size_type;

// ------------------------------------------------------------------ Str ----
class Str {
public:
    Str() : m_len(0) { m_buf[0] = '\0'; }

    Str(const char* s) : m_len(0) {
        while (s && s[m_len] && m_len < kCap - 1) {
            m_buf[m_len] = s[m_len];
            ++m_len;
        }
        m_buf[m_len] = '\0';
    }

    Str(const Str& other) : m_len(other.m_len) {
        for (size_type i = 0; i < m_len; ++i) {
            m_buf[i] = other.m_buf[i];
        }
        m_buf[m_len] = '\0';
    }

    Str& operator=(const Str& other) {
        if (this != &other) {
            m_len = other.m_len;
            for (size_type i = 0; i < m_len; ++i) {
                m_buf[i] = other.m_buf[i];
            }
            m_buf[m_len] = '\0';
        }
        return *this;
    }

    size_type size() const { return m_len; }
    bool empty() const { return m_len == 0; }
    const char* c_str() const { return m_buf; }

    char operator[](size_type i) const { return m_buf[i]; }
    char& operator[](size_type i) { return m_buf[i]; }

    Str operator+(const Str& other) const {
        Str out(*this);
        out.append(other);
        return out;
    }

    Str& operator+=(const Str& other) {
        append(other);
        return *this;
    }

    bool operator==(const Str& other) const {
        if (m_len != other.m_len) {
            return false;
        }
        for (size_type i = 0; i < m_len; ++i) {
            if (m_buf[i] != other.m_buf[i]) {
                return false;
            }
        }
        return true;
    }

    void append(const Str& other) {
        for (size_type i = 0; i < other.m_len && m_len < kCap - 1; ++i) {
            m_buf[m_len++] = other.m_buf[i];
        }
        m_buf[m_len] = '\0';
    }

    Str substr(size_type from, size_type count) const {
        Str out;
        for (size_type i = 0; i < count && from + i < m_len; ++i) {
            out.m_buf[out.m_len++] = m_buf[from + i];
        }
        out.m_buf[out.m_len] = '\0';
        return out;
    }

    bool hasPrefix(const Str& prefix) const {
        if (prefix.m_len > m_len) {
            return false;
        }
        for (size_type i = 0; i < prefix.m_len; ++i) {
            if (m_buf[i] != prefix.m_buf[i]) {
                return false;
            }
        }
        return true;
    }

private:
    static const size_type kCap = 256;
    char m_buf[kCap];
    size_type m_len;
};

// ------------------------------------------------------------------ Vec ----
template <typename T>
class Vec {
public:
    Vec() : m_size(0) {}

    void push_back(const T& v) {
        if (m_size < kCap) {
            m_items[m_size++] = v;
        }
    }

    void pop_back() {
        if (m_size > 0) {
            --m_size;
        }
    }

    void eraseAt(size_type i) {
        if (i >= m_size) {
            return;
        }
        for (size_type j = i; j + 1 < m_size; ++j) {
            m_items[j] = m_items[j + 1];
        }
        --m_size;
    }

    const T& operator[](size_type i) const { return m_items[i]; }
    T& operator[](size_type i) { return m_items[i]; }

    const T& back() const { return m_items[m_size - 1]; }
    bool empty() const { return m_size == 0; }
    size_type size() const { return m_size; }
    void clear() { m_size = 0; }

private:
    static const size_type kCap = 64;
    T m_items[kCap];
    size_type m_size;
};

// -------------------------------------------------------------- helpers ----
inline bool isSpaceChar(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

inline char toUpperChar(char c) {
    return (c >= 'a' && c <= 'z') ? static_cast<char>(c - 'a' + 'A') : c;
}

inline double absValue(double v) {
    return v < 0.0 ? -v : v;
}

// Newton's method -- avoids needing <cmath>
inline double sqrtValue(double v) {
    if (v <= 0.0) {
        return 0.0;
    }
    double guess = v;
    for (int i = 0; i < 24; ++i) {
        guess = 0.5 * (guess + v / guess);
    }
    return guess;
}

inline Str intToStr(int v) {
    if (v == 0) {
        return Str("0");
    }
    char tmp[32];
    int n = 0;
    bool neg = v < 0;
    unsigned int uv = neg ? static_cast<unsigned int>(-v) : static_cast<unsigned int>(v);
    while (uv > 0 && n < 30) {
        tmp[n++] = static_cast<char>('0' + (uv % 10));
        uv /= 10;
    }
    if (neg) {
        tmp[n++] = '-';
    }
    char out[32];
    int m = 0;
    while (n > 0) {
        out[m++] = tmp[--n];
    }
    out[m] = '\0';
    return Str(out);
}

}  // namespace corpus

#endif
