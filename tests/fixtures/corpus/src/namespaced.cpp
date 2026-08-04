#include "namespaced.h"
#include "prelude.h"

namespace app {
namespace net {

Socket::Socket() : m_fd(-1), m_open(false) {}

Socket::Socket(int fd) : m_fd(fd), m_open(fd >= 0) {}

Socket::~Socket() {}

bool Socket::open(const corpus::Str& host, int port) {
    if (host.empty() || port <= 0) {
        return false;
    }
    m_open = true;
    return true;
}

void Socket::close() {
    m_open = false;
    m_fd = -1;
}

int Socket::send(const char* data, int len) {
    if (!m_open || !data) {
        return -1;
    }
    return len;
}

int Socket::receive(char* buf, int len) {
    if (!m_open || !buf) {
        return -1;
    }
    return 0;
}

bool Socket::isOpen() const {
    return m_open;
}

int Socket::descriptor() const {
    return m_fd;
}

TcpSocket::TcpSocket() : Socket(), m_noDelay(false) {}

TcpSocket::~TcpSocket() {}

bool TcpSocket::open(const corpus::Str& host, int port) {
    if (!Socket::open(host, port)) {
        return false;
    }
    setNoDelay(true);
    return true;
}

void TcpSocket::close() {
    Socket::close();
    m_noDelay = false;
}

int TcpSocket::send(const char* data, int len) {
    return Socket::send(data, len);
}

void TcpSocket::setNoDelay(bool on) {
    m_noDelay = on;
}

bool TcpSocket::noDelay() const {
    return m_noDelay;
}

}  // namespace net

namespace audio {

Mixer::Mixer() : m_gain(1.0f), m_muted(false) {}

Mixer::~Mixer() {}

void Mixer::mix(float* out, int frames) {
    if (!out || m_muted) {
        return;
    }
    for (int i = 0; i < frames; ++i) {
        out[i] = out[i] * m_gain;
    }
}

void Mixer::setGain(float g) {
    m_gain = g;
}

float Mixer::gain() const {
    return m_gain;
}

void Mixer::mute() {
    m_muted = true;
}

bool Mixer::muted() const {
    return m_muted;
}

Player::Player() : Mixer(), m_playing(false) {}

Player::~Player() {}

void Player::mix(float* out, int frames) {
    Mixer::mix(out, frames);
}

corpus::Str Player::logName() const {
    return "player";
}

int Player::logLevel() const {
    return 2;
}

void Player::flushLog() {
    // nothing
}

void Player::play() {
    m_playing = true;
}

void Player::stop() {
    m_playing = false;
    flushLog();
}

bool Player::isPlaying() const {
    return m_playing;
}

}  // namespace audio

}  // namespace app
