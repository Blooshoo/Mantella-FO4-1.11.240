#pragma once

#pragma warning(push)
#include "F4SE/F4SE.h"
#include "RE/Fallout.h"

#define NDEBUG
#pragma warning(pop)

#define DLLEXPORT __declspec(dllexport)

// kalli/port-240: libxse/commonlibf4 7c8c6f8 has no F4SE::log; logging is REX::INFO/WARN/ERROR/CRITICAL,
// initialised by F4SE::Init (main.cpp kInitInfo). spdlog is libxse's own dependency, not used directly.

using namespace std::literals;

#include "Version.h"
