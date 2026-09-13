cmake_minimum_required(VERSION 3.16)

project(ProjectWatashi LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(USE_QT "Enable Qt UI" ON)
option(USE_ONNX "Enable ONNX Runtime backend" ON)
option(USE_LLAMA_CPP "Enable llama.cpp backend" ON)
option(USE_PADDLEOCR "Enable PaddleOCR backend" ON)
option(BUILD_TESTS "Build unit/integration tests" ON)

include_directories(
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/src
)

find_package(SQLite3 REQUIRED)
find_package(OpenCV REQUIRED)

if(USE_QT)
    find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets)
    set(CMAKE_AUTOMOC ON)
    set(CMAKE_AUTORCC ON)
    set(CMAKE_AUTOUIC ON)
endif()

if(USE_ONNX)
    find_package(onnxruntime REQUIRED)
endif()

if(USE_LLAMA_CPP)
    message(STATUS "llama.cpp backend enabled")
endif()

if(USE_PADDLEOCR)
    message(STATUS "PaddleOCR backend enabled")
endif()

file(GLOB_RECURSE CORE_SOURCES "${CMAKE_SOURCE_DIR}/src/core/*.cpp")
file(GLOB_RECURSE OCR_SOURCES "${CMAKE_SOURCE_DIR}/src/ocr/*.cpp")
file(GLOB_RECURSE LLM_SOURCES "${CMAKE_SOURCE_DIR}/src/llm/*.cpp")
file(GLOB_RECURSE DB_SOURCES "${CMAKE_SOURCE_DIR}/src/db/*.cpp")
file(GLOB_RECURSE RULES_SOURCES "${CMAKE_SOURCE_DIR}/src/rules/*.cpp")
file(GLOB_RECURSE PLUGINS_SOURCES "${CMAKE_SOURCE_DIR}/src/plugins/*.cpp")
file(GLOB_RECURSE UI_SOURCES "${CMAKE_SOURCE_DIR}/src/ui/*.cpp")
file(GLOB_RECURSE UTILS_SOURCES "${CMAKE_SOURCE_DIR}/src/utils/*.cpp")
file(GLOB_RECURSE COMMON_SOURCES "${CMAKE_SOURCE_DIR}/src/common/*.cpp")

set(APP_SOURCES
    ${CMAKE_SOURCE_DIR}/src/app/main.cpp
    ${CMAKE_SOURCE_DIR}/src/app/app.cpp
)

set(PROJECT_SOURCES
    ${CORE_SOURCES}
    ${OCR_SOURCES}
    ${LLM_SOURCES}
    ${DB_SOURCES}
    ${RULES_SOURCES}
    ${PLUGINS_SOURCES}
    ${UI_SOURCES}
    ${UTILS_SOURCES}
    ${COMMON_SOURCES}
    ${APP_SOURCES}
)

add_executable(ProjectWatashi ${PROJECT_SOURCES})

# Third-party libraries every target in this project needs, in one place.
#
# `tests/` links the same set. Duplicating the block there would let the two drift,
# and a test binary linked against a different ONNX Runtime or Qt than the
# application is a class of failure that wastes an afternoon.
set(PROJECT_WATASHI_LINK_LIBS
    SQLite::SQLite3
    ${OpenCV_LIBS}
)

if(USE_QT)
    list(APPEND PROJECT_WATASHI_LINK_LIBS
        Qt6::Core
        Qt6::Gui
        Qt6::Widgets
    )
endif()

if(USE_ONNX)
    list(APPEND PROJECT_WATASHI_LINK_LIBS onnxruntime)
endif()

target_link_libraries(ProjectWatashi PRIVATE ${PROJECT_WATASHI_LINK_LIBS})

if(USE_LLAMA_CPP)
    message(STATUS "Link with llama.cpp backend if available")
endif()

if(USE_PADDLEOCR)
    message(STATUS "Link with PaddleOCR backend if available")
endif()

target_compile_definitions(ProjectWatashi PRIVATE PROJECT_WATASHI_BUILD)

if(BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()
