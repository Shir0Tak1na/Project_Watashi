cmake_minimum_required(VERSION 3.20)
project(ocr_probe LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Point this at an official Microsoft.ML.OnnxRuntime NuGet package:
#   -DORT_ROOT="C:/path/to/microsoft.ml.onnxruntime.1.30.0"
# The probe deliberately does not link the Python wheel's onnxruntime.dll.
if(NOT ORT_ROOT)
    message(FATAL_ERROR "Set -DORT_ROOT to an extracted Microsoft.ML.OnnxRuntime package")
endif()

find_path(ORT_INCLUDE_DIR
    NAMES onnxruntime_cxx_api.h
    HINTS "${ORT_ROOT}/build/native/include"
    REQUIRED)
find_library(ORT_LIBRARY
    NAMES onnxruntime
    HINTS "${ORT_ROOT}/runtimes/win-x64/native" "${ORT_ROOT}/lib"
    REQUIRED)

add_executable(ocr_probe ocr_probe.cpp)
target_include_directories(ocr_probe PRIVATE "${ORT_INCLUDE_DIR}")
target_link_libraries(ocr_probe PRIVATE "${ORT_LIBRARY}")

if(WIN32)
    add_custom_command(TARGET ocr_probe POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${ORT_ROOT}/runtimes/win-x64/native/onnxruntime.dll"
            "$<TARGET_FILE_DIR:ocr_probe>/onnxruntime.dll"
        COMMENT "Copying onnxruntime.dll next to ocr_probe.exe")
endif()
