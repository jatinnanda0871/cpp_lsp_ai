#include "shapes.h"
#include "prelude.h"

namespace geo {

Shape::Shape() : m_id(0) {}

Shape::Shape(int id) : m_id(id) {}

Shape::~Shape() {}

// virtual with a definition -- note the body spans several lines, so the
// "end line" reported for it must come from documentSymbol's full range and
// not from the identifier line alone.
double Shape::perimeter() const {
    double base = 0.0;
    if (m_id > 0) {
        base = static_cast<double>(m_id);
    }
    return base;
}

void Shape::describe() const {
    double a = area();
    double p = perimeter();
    (void)a;
    (void)p;
}

int Shape::id() const {
    return m_id;
}

void Shape::setId(int id) {
    m_id = id;
}

bool Shape::isValid() const {
    return m_id >= 0;
}

Circle::Circle(double r) : Shape(1), m_r(r) {}

Circle::~Circle() {}

double Circle::area() const {
    return 3.14159265358979 * m_r * m_r;
}

corpus::Str Circle::name() const {
    return "circle";
}

double Circle::perimeter() const {
    return 2.0 * 3.14159265358979 * m_r;
}

double Circle::radius() const {
    return m_r;
}

void Circle::scale(double factor) {
    m_r = m_r * factor;
}

double Circle::diameter() const {
    return radius() * 2.0;
}

Rectangle::Rectangle(double w, double h) : Shape(2), m_w(w), m_h(h) {}

Rectangle::~Rectangle() {}

double Rectangle::area() const {
    return m_w * m_h;
}

corpus::Str Rectangle::name() const {
    return "rectangle";
}

double Rectangle::perimeter() const {
    return 2.0 * (m_w + m_h);
}

double Rectangle::width() const {
    return m_w;
}

double Rectangle::height() const {
    return m_h;
}

void Rectangle::resize(double w, double h) {
    m_w = w;
    m_h = h;
}

bool Rectangle::isSquare() const {
    return corpus::absValue(m_w - m_h) < 1e-9;
}

Square::Square(double side) : Rectangle(side, side) {}

Square::~Square() {}

corpus::Str Square::name() const {
    return "square";
}

void Square::setSide(double side) {
    resize(side, side);
}

double Square::side() const {
    return width();
}

Triangle::Triangle(double a, double b, double c)
    : Shape(3), m_a(a), m_b(b), m_c(c) {}

Triangle::~Triangle() {}

double Triangle::area() const {
    double s = (m_a + m_b + m_c) / 2.0;
    return corpus::sqrtValue(s * (s - m_a) * (s - m_b) * (s - m_c));
}

corpus::Str Triangle::name() const {
    return "triangle";
}

double Triangle::perimeter() const {
    return m_a + m_b + m_c;
}

bool Triangle::isEquilateral() const {
    return corpus::absValue(m_a - m_b) < 1e-9 && corpus::absValue(m_b - m_c) < 1e-9;
}

double totalArea(const Shape** shapes, int count) {
    double sum = 0.0;
    for (int i = 0; i < count; ++i) {
        sum += shapes[i]->area();
    }
    return sum;
}

const char* shapeKindName(int kind) {
    switch (kind) {
        case 1: return "circle";
        case 2: return "rectangle";
        case 3: return "triangle";
        default: return "unknown";
    }
}

bool compareShapes(const Shape& a, const Shape& b) {
    return a.area() < b.area();
}

}  // namespace geo
