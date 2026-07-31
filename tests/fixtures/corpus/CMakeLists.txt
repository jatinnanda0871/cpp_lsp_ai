# Test corpus for the clangq query engine.
#
# Only purpose is to produce a compile_commands.json that clangd can index:
#   cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
#
# tests/support.py runs this automatically when cmake is available, and falls
# back to writing compile_commands.json directly when it is not (clangd needs
# the compile flags, not a built binary).
cmake_minimum_required(VERSION 3.10)
project(clangq_test_corpus CXX)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)

add_executable(corpus
    src/shapes.cpp
    src/overloads.cpp
    src/containers.cpp
    src/namespaced.cpp
    src/util.cpp
    src/chains.cpp
    src/big.cpp
    src/main.cpp
)
