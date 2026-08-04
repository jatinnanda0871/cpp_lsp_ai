// A deliberately LARGE class with long method bodies.
// The point is the reported def start/end line span: a long body must report
// an end line far from its start, which only works if the end comes from
// documentSymbol's full range rather than the definition's identifier range.
#ifndef CORPUS_BIG_H
#define CORPUS_BIG_H

#include "prelude.h"


namespace big {

class BigService {
public:
    BigService();
    ~BigService();

    // ~40-line body in big.cpp
    int runPipeline(const corpus::Vec<int>& input, corpus::Vec<int>* output);

    // ~30-line body
    corpus::Str formatReport(const corpus::Vec<int>& data) const;

    // ~25-line body
    bool validateConfig(int maxItems, int timeout, bool strict);

    // one-line bodies, for contrast with the long ones above
    int quickAdd(int a, int b);
    int quickSub(int a, int b);
    bool quickCheck(int v);
    void quickReset();
    int quickValue() const;

    void setLimit(int limit);
    int limit() const;
    void enableStrict(bool on);
    bool strict() const;
    int errorCount() const;
    void clearErrors();

private:
    int m_limit;
    bool m_strict;
    int m_errors;
    int m_value;
};

// large free function, ~35 lines
int computeChecksum(const corpus::Vec<int>& data, int seed);

// small free functions
int tinyOne();
int tinyTwo();
int tinyThree();

}  // namespace big

#endif
