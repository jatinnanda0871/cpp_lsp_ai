// Ties the corpus together and creates real reference/call sites so that
// references and incoming-call queries have something to find.
#include "big.h"
#include "prelude.h"
#include "chains.h"
#include "containers.h"
#include "inline_only.h"
#include "namespaced.h"
#include "overloads.h"
#include "shapes.h"
#include "util.h"


int main() {
    // shapes: virtual dispatch through the abstract base
    geo::Circle circle(2.0);
    geo::Rectangle rect(3.0, 4.0);
    geo::Square square(5.0);
    geo::Triangle tri(3.0, 4.0, 5.0);

    circle.scale(2.0);
    double d = circle.diameter();
    rect.resize(6.0, 8.0);
    bool sq = rect.isSquare();
    square.setSide(7.0);
    double side = square.side();
    bool eq = tri.isEquilateral();
    circle.describe();
    rect.describe();

    const geo::Shape* shapes[2];
    shapes[0] = &circle;
    shapes[1] = &rect;
    double total = geo::totalArea(shapes, 2);
    const char* kind = geo::shapeKindName(1);
    bool cmp = geo::compareShapes(circle, rect);

    // header-only classes
    util::Vec2 v(1.0, 2.0);
    v.setX(3.0);
    double ls = v.lengthSquared();
    util::Vec2 sum = v.added(util::Vec2(1.0, 1.0));
    bool zero = sum.isZero();
    v.reset();

    util::Counter counter(2);
    counter.bump(5);
    int cv = counter.value();
    bool at = counter.atLeast(4);
    counter.decrement();
    counter.reset();

    // overload sets: each call picks a different overload
    calc::Calculator calcr;
    int i1 = calcr.add(1, 2);
    double d1 = calcr.add(1.5, 2.5);
    int i2 = calcr.add(1, 2, 3);
    corpus::Str s1 = calcr.add(corpus::Str("a"), corpus::Str("b"));
    calcr.accumulate(10);
    int t1 = calcr.total();
    calcr.clear();
    int st = calc::Calculator::staticAdd(4, 5);
    int m1 = calc::maxOf(1, 2);
    double m2 = calc::maxOf(1.5, 2.5);
    int m3 = calc::maxOf(1, 2, 3);

    calc::Matrix mat(2, 2);
    mat.fill(1.0);
    double tr = mat.trace();
    int mr = mat.rows();

    // templates
    cont::Stack<int> stack;
    stack.push(1);
    stack.push(2);
    stack.pop();
    bool se = stack.empty();
    int ss = stack.size();

    cont::Pair<int, corpus::Str> pair(1, "one");
    int pf = pair.first();
    pair.setFirst(2);
    int idv = cont::identity(42);

    cont::Registry reg;
    reg.insert("a", 1);
    bool has = reg.contains("a");
    int look = reg.lookup("a");
    reg.removeKey("a");
    int rc = reg.count();

    // namespaces + multiple inheritance
    app::net::TcpSocket sock;
    sock.open("localhost", 8080);
    sock.send("hi", 2);
    bool nd = sock.noDelay();
    sock.close();

    app::audio::Player player;
    player.setGain(0.5f);
    player.play();
    bool pl = player.isPlaying();
    corpus::Str ln = player.logName();
    player.stop();

    // macros / structs / free functions
    Config cfg;
    cfg.applyDefaults();
    bool cv2 = cfg.isValid();
    int clamped = util::clampInt(200, 0, CORPUS_MAX_ITEMS);
    double clampedD = util::clampDouble(1.5, 0.0, 1.0);
    int direct = CORPUS_CLAMP(5, 0, 3);
    corpus::Str tr2 = util::trim("  x  ");
    corpus::Str up = util::toUpper("abc");
    bool sw = util::startsWith("abcdef", "abc");
    bool ok = false;
    int pi = util::parseInt("123", &ok);
    int sr = util::sumRange(1, 5);
    int a = 1, b = 2;
    util::swapInts(&a, &b);
    Point pt;
    pt.x = 1;
    pt.y = 2;
    GeoCoord gc;
    gc.lat = 0.0;
    gc.lon = 0.0;

    // call chains with known caller counts
    chain::Backend backend;
    chain::Frontend frontend;
    frontend.setBackend(&backend);
    int r1 = frontend.handleRequest("abc");
    int r2 = frontend.handleBatch("def");
    int r3 = frontend.retry("ghi");
    int pc = backend.processedCount();
    backend.resetCount();

    // big service
    big::BigService svc;
    corpus::Vec<int> input;
    input.push_back(1);
    input.push_back(2);
    corpus::Vec<int> output;
    int processed = svc.runPipeline(input, &output);
    corpus::Str report = svc.formatReport(input);
    bool vc = svc.validateConfig(50, 30, true);
    int qa = svc.quickAdd(1, 2);
    int qs = svc.quickSub(3, 1);
    bool qc = svc.quickCheck(5);
    svc.quickReset();
    int qv = svc.quickValue();
    svc.setLimit(10);
    int lim = svc.limit();
    svc.enableStrict(true);
    bool strict = svc.strict();
    int ec = svc.errorCount();
    svc.clearErrors();
    int chk = big::computeChecksum(input, 7);
    int t_1 = big::tinyOne();
    int t_2 = big::tinyTwo();
    int t_3 = big::tinyThree();

    (void)d; (void)sq; (void)side; (void)eq; (void)total; (void)kind; (void)cmp;
    (void)ls; (void)zero; (void)cv; (void)at;
    (void)i1; (void)d1; (void)i2; (void)s1; (void)t1; (void)st;
    (void)m1; (void)m2; (void)m3; (void)tr; (void)mr;
    (void)se; (void)ss; (void)pf; (void)idv; (void)has; (void)look; (void)rc;
    (void)nd; (void)pl; (void)ln;
    (void)cv2; (void)clamped; (void)clampedD; (void)direct; (void)tr2; (void)up;
    (void)sw; (void)pi; (void)sr; (void)a; (void)b;
    (void)r1; (void)r2; (void)r3; (void)pc;
    (void)processed; (void)report; (void)vc; (void)qa; (void)qs; (void)qc;
    (void)qv; (void)lim; (void)strict; (void)ec; (void)chk;
    (void)t_1; (void)t_2; (void)t_3;
    return 0;
}
