// Virtual / pure-virtual hierarchy.
// Every method here is DECLARED in this header and DEFINED in src/shapes.cpp,
// which is the case that makes prepareCallHierarchy fail if a query anchors on
// the declaration instead of resolving the definition first.
#ifndef CORPUS_SHAPES_H
#define CORPUS_SHAPES_H

#include "prelude.h"


namespace geo {

// Abstract base: area() and name() are pure virtual and have NO definition
// anywhere. Queries for them must degrade gracefully.
class Shape {
public:
    Shape();
    explicit Shape(int id);
    virtual ~Shape();

    virtual double area() const = 0;
    virtual corpus::Str name() const = 0;

    // virtual with a real definition, overridden below
    virtual double perimeter() const;
    virtual void describe() const;

    int id() const;
    void setId(int id);
    bool isValid() const;

protected:
    int m_id;
};

class Circle : public Shape {
public:
    explicit Circle(double r);
    ~Circle();

    double area() const override;
    corpus::Str name() const override;
    double perimeter() const override;

    double radius() const;
    void scale(double factor);
    double diameter() const;

private:
    double m_r;
};

class Rectangle : public Shape {
public:
    Rectangle(double w, double h);
    ~Rectangle();

    double area() const override;
    corpus::Str name() const override;
    double perimeter() const override;

    double width() const;
    double height() const;
    void resize(double w, double h);
    bool isSquare() const;

protected:
    double m_w;
    double m_h;
};

// Derives from a derived class -- three levels deep.
class Square : public Rectangle {
public:
    explicit Square(double side);
    ~Square();

    corpus::Str name() const override;
    void setSide(double side);
    double side() const;
};

class Triangle : public Shape {
public:
    Triangle(double a, double b, double c);
    ~Triangle();

    double area() const override;
    corpus::Str name() const override;
    double perimeter() const override;
    bool isEquilateral() const;

private:
    double m_a, m_b, m_c;
};

// Free functions declared here, defined in shapes.cpp
double totalArea(const Shape** shapes, int count);
const char* shapeKindName(int kind);
bool compareShapes(const Shape& a, const Shape& b);

}  // namespace geo

#endif
