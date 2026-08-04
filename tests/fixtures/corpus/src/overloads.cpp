#include "overloads.h"
#include "prelude.h"

namespace calc {

Calculator::Calculator() : m_total(0) {}

Calculator::~Calculator() {}

int Calculator::add(int a, int b) {
    return a + b;
}

double Calculator::add(double a, double b) {
    return a + b;
}

int Calculator::add(int a, int b, int c) {
    return add(a, b) + c;
}

corpus::Str Calculator::add(const corpus::Str& a, const corpus::Str& b) {
    return a + b;
}

int Calculator::value() {
    return m_total;
}

int Calculator::value() const {
    return m_total;
}

int Calculator::staticAdd(int a, int b) {
    return a + b;
}

Calculator* Calculator::create() {
    return new Calculator();
}

void Calculator::accumulate(int n) {
    m_total = add(m_total, n);
}

int Calculator::total() const {
    return m_total;
}

void Calculator::clear() {
    m_total = 0;
}

Matrix::Matrix() : m_rows(0), m_cols(0), m_data(0) {}

Matrix::Matrix(int rows, int cols) : m_rows(rows), m_cols(cols), m_data(0) {
    if (rows > 0 && cols > 0) {
        m_data = new double[rows * cols];
        for (int i = 0; i < rows * cols; ++i) {
            m_data[i] = 0.0;
        }
    }
}

Matrix::~Matrix() {
    delete[] m_data;
}

Matrix Matrix::operator+(const Matrix& other) const {
    Matrix out(m_rows, m_cols);
    if (m_data && other.m_data && out.m_data) {
        for (int i = 0; i < m_rows * m_cols; ++i) {
            out.m_data[i] = m_data[i] + other.m_data[i];
        }
    }
    return out;
}

Matrix Matrix::operator*(const Matrix& other) const {
    Matrix out(m_rows, other.m_cols);
    return out;
}

bool Matrix::operator==(const Matrix& other) const {
    return m_rows == other.m_rows && m_cols == other.m_cols;
}

double& Matrix::operator()(int r, int c) {
    return m_data[r * m_cols + c];
}

int Matrix::rows() const {
    return m_rows;
}

int Matrix::cols() const {
    return m_cols;
}

void Matrix::fill(double v) {
    if (!m_data) {
        return;
    }
    for (int i = 0; i < m_rows * m_cols; ++i) {
        m_data[i] = v;
    }
}

double Matrix::trace() const {
    double sum = 0.0;
    int n = m_rows < m_cols ? m_rows : m_cols;
    for (int i = 0; i < n; ++i) {
        sum += m_data[i * m_cols + i];
    }
    return sum;
}

int maxOf(int a, int b) {
    return a > b ? a : b;
}

double maxOf(double a, double b) {
    return a > b ? a : b;
}

int maxOf(int a, int b, int c) {
    return maxOf(maxOf(a, b), c);
}

}  // namespace calc
