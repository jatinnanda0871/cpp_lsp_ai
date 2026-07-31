// Deliberate call chains with KNOWN caller counts, so incoming-call queries
// can be checked against exact ground truth rather than "did not crash".
//
// Call graph (see chains.cpp):
//   Frontend::handleRequest -> Backend::process        (1 caller)
//   Frontend::handleBatch   -> Backend::process        (2nd caller)
//   Frontend::retry         -> Backend::process        (3rd caller)
//   Backend::process        -> Backend::validate       (1 caller)
//   Backend::validate       -> (leaf, 1 caller)
//   Backend::orphan         -> (never called: 0 callers)
#ifndef CORPUS_CHAINS_H
#define CORPUS_CHAINS_H

#include "prelude.h"


namespace chain {

class Backend {
public:
    Backend();
    ~Backend();

    int process(const corpus::Str& payload);
    bool validate(const corpus::Str& payload);

    // never called by anything -- exact 0-caller case
    void orphan();

    int processedCount() const;
    void resetCount();

private:
    int m_count;
};

class Frontend {
public:
    Frontend();
    ~Frontend();

    // each of these calls Backend::process exactly once
    int handleRequest(const corpus::Str& payload);
    int handleBatch(const corpus::Str& payload);
    int retry(const corpus::Str& payload);

    void setBackend(Backend* b);
    Backend* backend() const;

private:
    Backend* m_backend;
};

}  // namespace chain

#endif
