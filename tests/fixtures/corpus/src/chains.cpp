#include "chains.h"
#include "prelude.h"

namespace chain {

Backend::Backend() : m_count(0) {}

Backend::~Backend() {}

int Backend::process(const corpus::Str& payload) {
    if (!validate(payload)) {
        return -1;
    }
    ++m_count;
    return static_cast<int>(payload.size());
}

bool Backend::validate(const corpus::Str& payload) {
    return !payload.empty();
}

void Backend::orphan() {
    // deliberately never called
}

int Backend::processedCount() const {
    return m_count;
}

void Backend::resetCount() {
    m_count = 0;
}

Frontend::Frontend() : m_backend(0) {}

Frontend::~Frontend() {}

int Frontend::handleRequest(const corpus::Str& payload) {
    if (!m_backend) {
        return -1;
    }
    return m_backend->process(payload);
}

int Frontend::handleBatch(const corpus::Str& payload) {
    if (!m_backend) {
        return -1;
    }
    int total = 0;
    total += m_backend->process(payload);
    return total;
}

int Frontend::retry(const corpus::Str& payload) {
    if (!m_backend) {
        return -1;
    }
    return m_backend->process(payload);
}

void Frontend::setBackend(Backend* b) {
    m_backend = b;
}

Backend* Frontend::backend() const {
    return m_backend;
}

}  // namespace chain
