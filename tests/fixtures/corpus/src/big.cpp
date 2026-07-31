#include "big.h"
#include "prelude.h"

namespace big {

BigService::BigService()
    : m_limit(100), m_strict(false), m_errors(0), m_value(0) {}

BigService::~BigService() {}

// Long body on purpose: the reported end line must be ~40 lines past the start.
int BigService::runPipeline(const corpus::Vec<int>& input, corpus::Vec<int>* output) {
    if (!output) {
        ++m_errors;
        return -1;
    }
    output->clear();
    if (input.empty()) {
        return 0;
    }
    int processed = 0;
    for (corpus::size_type i = 0; i < input.size(); ++i) {
        int v = input[i];
        if (m_strict && v < 0) {
            ++m_errors;
            continue;
        }
        if (v > m_limit) {
            v = m_limit;
        }
        if (v < 0) {
            v = 0;
        }
        int doubled = v * 2;
        int adjusted = doubled;
        if (adjusted % 3 == 0) {
            adjusted += 1;
        } else if (adjusted % 5 == 0) {
            adjusted -= 1;
        }
        output->push_back(adjusted);
        ++processed;
        if (processed >= m_limit) {
            break;
        }
    }
    m_value = processed;
    return processed;
}

corpus::Str BigService::formatReport(const corpus::Vec<int>& data) const {
    corpus::Str out;
    out += "report:\n";
    if (data.empty()) {
        out += "  (empty)\n";
        return out;
    }
    int total = 0;
    int maxSeen = data[0];
    int minSeen = data[0];
    for (corpus::size_type i = 0; i < data.size(); ++i) {
        int v = data[i];
        total += v;
        if (v > maxSeen) {
            maxSeen = v;
        }
        if (v < minSeen) {
            minSeen = v;
        }
    }
    out += "  count=";
    out += corpus::intToStr(data.size());
    out += "\n  total=";
    out += corpus::intToStr(total);
    out += "\n  max=";
    out += corpus::intToStr(maxSeen);
    out += "\n  min=";
    out += corpus::intToStr(minSeen);
    out += "\n";
    return out;
}

bool BigService::validateConfig(int maxItems, int timeout, bool strict) {
    if (maxItems <= 0) {
        ++m_errors;
        return false;
    }
    if (maxItems > 10000) {
        ++m_errors;
        return false;
    }
    if (timeout < 0) {
        ++m_errors;
        return false;
    }
    if (timeout > 3600) {
        ++m_errors;
        return false;
    }
    if (strict && maxItems > 1000) {
        ++m_errors;
        return false;
    }
    m_limit = maxItems;
    m_strict = strict;
    return true;
}

int BigService::quickAdd(int a, int b) { return a + b; }

int BigService::quickSub(int a, int b) { return a - b; }

bool BigService::quickCheck(int v) { return v <= m_limit; }

void BigService::quickReset() { m_value = 0; }

int BigService::quickValue() const { return m_value; }

void BigService::setLimit(int limit) {
    m_limit = limit;
}

int BigService::limit() const {
    return m_limit;
}

void BigService::enableStrict(bool on) {
    m_strict = on;
}

bool BigService::strict() const {
    return m_strict;
}

int BigService::errorCount() const {
    return m_errors;
}

void BigService::clearErrors() {
    m_errors = 0;
}

int computeChecksum(const corpus::Vec<int>& data, int seed) {
    int sum = seed;
    if (data.empty()) {
        return sum;
    }
    for (corpus::size_type i = 0; i < data.size(); ++i) {
        int v = data[i];
        sum = sum * 31 + v;
        if (sum > 1000000) {
            sum = sum % 1000000;
        }
        if (sum < 0) {
            sum = -sum;
        }
        if (i % 2 == 0) {
            sum += 7;
        } else {
            sum -= 3;
        }
        if (sum == 0) {
            sum = 1;
        }
    }
    int finalAdjust = static_cast<int>(data.size()) % 17;
    sum += finalAdjust;
    return sum;
}

int tinyOne() { return 1; }

int tinyTwo() { return 2; }

int tinyThree() { return 3; }

}  // namespace big
