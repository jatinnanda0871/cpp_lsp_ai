// Templates and nested classes.
// Template methods must be defined in the header, so decl and def coincide,
// but clangd reports template symbols differently from plain classes.
// Registry::Entry is a NESTED class -- documentSymbol returns it as a child of
// a child, which exercises the recursive node search.
#ifndef CORPUS_CONTAINERS_H
#define CORPUS_CONTAINERS_H

#include "prelude.h"


namespace cont {

template <typename T>
class Stack {
public:
    Stack() : m_items() {}

    void push(const T& item) {
        m_items.push_back(item);
    }

    void pop() {
        if (!m_items.empty()) {
            m_items.pop_back();
        }
    }

    const T& top() const {
        return m_items.back();
    }

    bool empty() const {
        return m_items.empty();
    }

    int size() const {
        return static_cast<int>(m_items.size());
    }

    void clear() {
        m_items.clear();
    }

private:
    corpus::Vec<T> m_items;
};

template <typename A, typename B>
class Pair {
public:
    Pair() : m_first(), m_second() {}
    Pair(const A& a, const B& b) : m_first(a), m_second(b) {}

    const A& first() const { return m_first; }
    const B& second() const { return m_second; }

    void setFirst(const A& a) { m_first = a; }
    void setSecond(const B& b) { m_second = b; }

    void swapWith(Pair& other) {
        A ta = m_first;
        m_first = other.m_first;
        other.m_first = ta;
    }

private:
    A m_first;
    B m_second;
};

// Non-template class containing a NESTED class.
class Registry {
public:
    class Entry {
    public:
        Entry();
        Entry(const corpus::Str& key, int value);

        const corpus::Str& key() const;
        int value() const;
        void setValue(int v);
        bool matches(const corpus::Str& k) const;

    private:
        corpus::Str m_key;
        int m_value;
    };

    Registry();
    ~Registry();

    void insert(const corpus::Str& key, int value);
    bool contains(const corpus::Str& key) const;
    int lookup(const corpus::Str& key) const;
    void removeKey(const corpus::Str& key);
    int count() const;
    void clearAll();

private:
    corpus::Vec<Entry> m_entries;
};

// free function template
template <typename T>
T identity(const T& v) {
    return v;
}

}  // namespace cont

#endif
