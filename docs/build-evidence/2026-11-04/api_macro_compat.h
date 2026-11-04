#pragma once
// Build-only include ordering: parse libxse before Windows macros, then prevent
// spdlog Windows headers from replacing qualified REX constants/logging names.
#include <F4SE/Impl/PCH.h>
#include <spdlog/sinks/basic_file_sink.h>
#include <spdlog/sinks/msvc_sink.h>
#include <spdlog/sinks/rotating_file_sink.h>
#include <spdlog/spdlog.h>
#undef MAX_PATH
#undef ERROR
