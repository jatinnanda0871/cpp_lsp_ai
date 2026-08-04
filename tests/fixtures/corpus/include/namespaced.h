// Nested namespaces, a pure interface, and multiple inheritance.
// Player inherits from TWO bases, so its documentSymbol children include
// overrides of methods declared in different files.
#ifndef CORPUS_NAMESPACED_H
#define CORPUS_NAMESPACED_H

#include "prelude.h"


namespace app {

// Pure interface: every method is pure virtual, no definitions at all.
class Loggable {
public:
    virtual ~Loggable() {}
    virtual corpus::Str logName() const = 0;
    virtual int logLevel() const = 0;
    virtual void flushLog() = 0;
};

namespace net {

class Socket {
public:
    Socket();
    explicit Socket(int fd);
    virtual ~Socket();

    virtual bool open(const corpus::Str& host, int port);
    virtual void close();
    virtual int send(const char* data, int len);
    virtual int receive(char* buf, int len);

    bool isOpen() const;
    int descriptor() const;

protected:
    int m_fd;
    bool m_open;
};

class TcpSocket : public Socket {
public:
    TcpSocket();
    ~TcpSocket();

    bool open(const corpus::Str& host, int port) override;
    void close() override;
    int send(const char* data, int len) override;

    void setNoDelay(bool on);
    bool noDelay() const;

private:
    bool m_noDelay;
};

}  // namespace net

namespace audio {

class Mixer {
public:
    Mixer();
    virtual ~Mixer();

    virtual void mix(float* out, int frames);
    void setGain(float g);
    float gain() const;
    void mute();
    bool muted() const;

protected:
    float m_gain;
    bool m_muted;
};

// Multiple inheritance: concrete Mixer + pure-virtual Loggable.
class Player : public Mixer, public Loggable {
public:
    Player();
    ~Player();

    void mix(float* out, int frames) override;

    corpus::Str logName() const override;
    int logLevel() const override;
    void flushLog() override;

    void play();
    void stop();
    bool isPlaying() const;

private:
    bool m_playing;
};

}  // namespace audio

}  // namespace app

#endif
