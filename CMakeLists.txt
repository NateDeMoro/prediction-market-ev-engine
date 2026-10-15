# Open when: changing how the C++ pricing core is built or tested.
# Standalone: cmake -S . -B build -DPMEV_BUILD_TESTS=ON && ctest --test-dir build
# Python module: built only when scikit-build-core drives the build (pip install .).
cmake_minimum_required(VERSION 3.15...3.30)
project(pmev_pricing LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(PMEV_BUILD_TESTS "Build the GoogleTest suite" OFF)

# Python's float arithmetic never fuses multiply-add and has no fast-math, so
# neither may the C++ port or results drift in the last bits.
add_compile_options(-ffp-contract=off)

add_library(pmev_pricing_core STATIC cpp/src/pricing.cpp)
target_include_directories(pmev_pricing_core PUBLIC cpp/include)

if(SKBUILD)
  find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
  find_package(pybind11 CONFIG REQUIRED)
  pybind11_add_module(pmev_pricing cpp/bindings/module.cpp)
  target_link_libraries(pmev_pricing PRIVATE pmev_pricing_core)
  install(TARGETS pmev_pricing LIBRARY DESTINATION .)
endif()

if(PMEV_BUILD_TESTS)
  include(FetchContent)
  FetchContent_Declare(googletest
    URL https://github.com/google/googletest/archive/refs/tags/v1.15.2.tar.gz)
  set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
  FetchContent_MakeAvailable(googletest)
  enable_testing()
  add_executable(test_pricing cpp/tests/test_pricing.cpp)
  target_link_libraries(test_pricing PRIVATE pmev_pricing_core GTest::gtest_main)
  include(GoogleTest)
  gtest_discover_tests(test_pricing)
endif()
