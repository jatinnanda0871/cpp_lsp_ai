// Overload sets, operators, static and const methods.
// Overloads are the case where workspace/symbol returns MULTIPLE hits for one
// name, so a query must handle N candidate positions rather than assuming one.
#ifndef CORPUS_OVERLOADS_H
#define CORPUS_OVERLOADS_H

#include "prelude.h"


namespace calc {

class Calculator {
public:
    Calculator();
    ~Calculator();

    // four-way overload set, all defined in overloads.cpp
    int add(int a, int b);
    double add(double a, double b);
    int add(int a, int b, int c);
    corpus::Str add(const corpus::Str& a, const corpus::Str& b);

    // const vs non-const overload pair on the same name
    int value();
    int value() const;

    static int staticAdd(int a, int b);
    static Calculator* create();

    void accumulate(int n);
    int total() const;
    void clear();

private:
    int m_total;
};

class Matrix {
public:
    Matrix();
    Matrix(int rows, int cols);
    ~Matrix();

    Matrix operator+(const Matrix& other) const;
    Matrix operator*(const Matrix& other) const;
    bool operator==(const Matrix& other) const;
    double& operator()(int r, int c);

    int rows() const;
    int cols() const;
    void fill(double v);
    double trace() const;

private:
    int m_rows;
    int m_cols;
    double* m_data;
};

// free-function overload set
int maxOf(int a, int b);
double maxOf(double a, double b);
int maxOf(int a, int b, int c);

}  // namespace calc

#endif
