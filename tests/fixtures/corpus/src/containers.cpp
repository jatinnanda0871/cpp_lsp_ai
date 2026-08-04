#include "containers.h"
#include "prelude.h"

namespace cont {

Registry::Entry::Entry() : m_key(), m_value(0) {}

Registry::Entry::Entry(const corpus::Str& key, int value)
    : m_key(key), m_value(value) {}

const corpus::Str& Registry::Entry::key() const {
    return m_key;
}

int Registry::Entry::value() const {
    return m_value;
}

void Registry::Entry::setValue(int v) {
    m_value = v;
}

bool Registry::Entry::matches(const corpus::Str& k) const {
    return m_key == k;
}

Registry::Registry() : m_entries() {}

Registry::~Registry() {}

void Registry::insert(const corpus::Str& key, int value) {
    for (corpus::size_type i = 0; i < m_entries.size(); ++i) {
        if (m_entries[i].matches(key)) {
            m_entries[i].setValue(value);
            return;
        }
    }
    m_entries.push_back(Entry(key, value));
}

bool Registry::contains(const corpus::Str& key) const {
    for (corpus::size_type i = 0; i < m_entries.size(); ++i) {
        if (m_entries[i].matches(key)) {
            return true;
        }
    }
    return false;
}

int Registry::lookup(const corpus::Str& key) const {
    for (corpus::size_type i = 0; i < m_entries.size(); ++i) {
        if (m_entries[i].matches(key)) {
            return m_entries[i].value();
        }
    }
    return -1;
}

void Registry::removeKey(const corpus::Str& key) {
    for (corpus::size_type i = 0; i < m_entries.size(); ++i) {
        if (m_entries[i].matches(key)) {
            m_entries.eraseAt(i);
            return;
        }
    }
}

int Registry::count() const {
    return static_cast<int>(m_entries.size());
}

void Registry::clearAll() {
    m_entries.clear();
}

}  // namespace cont
