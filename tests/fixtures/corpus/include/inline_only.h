// Header-only classes: declaration and definition are the SAME location.
// The opposite of shapes.h -- here textDocument/definition resolves to the
// in-class body, so def and decl lines coincide.
#ifndef CORPUS_INLINE_ONLY_H
#define CORPUS_INLINE_ONLY_H

#include "prelude.h"

namespace util {

class Vec2 {
public:
    Vec2() : m_x(0.0), m_y(0.0) {}
    Vec2(double x, double y) : m_x(x), m_y(y) {}

    double x() const { return m_x; }
    double y() const { return m_y; }

    void setX(double x) { m_x = x; }
    void setY(double y) { m_y = y; }

    // multi-line inline body
    double lengthSquared() const {
        double xx = m_x * m_x;
        double yy = m_y * m_y;
        return xx + yy;
    }

    Vec2 added(const Vec2& other) const {
        return Vec2(m_x + other.m_x, m_y + other.m_y);
    }

    void reset() {
        m_x = 0.0;
        m_y = 0.0;
    }

    bool isZero() const { return m_x == 0.0 && m_y == 0.0; }

private:
    double m_x;
    double m_y;
};

class Counter {
public:
    Counter() : m_count(0), m_step(1) {}
    explicit Counter(int step) : m_count(0), m_step(step) {}

    void increment() { m_count += m_step; }
    void decrement() { m_count -= m_step; }
    int value() const { return m_count; }
    void reset() { m_count = 0; }

    // calls another inline method -- exercises incoming-call hierarchy
    // entirely within a header
    void bump(int times) {
        for (int i = 0; i < times; ++i) {
            increment();
        }
    }

    bool atLeast(int threshold) const {
        return value() >= threshold;
    }

private:
    int m_count;
    int m_step;
};

}  // namespace util

#endif
