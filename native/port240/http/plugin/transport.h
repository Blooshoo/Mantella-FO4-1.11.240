// Win32-only helpers for F4SE_HTTP240, kept in their own translation unit (transport_winhttp.cpp)
// so <Windows.h>/<winhttp.h> never meet libxse's own Win32 declarations.
#pragma once

#include "f4sehttp240/http_core.h"

#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

namespace f4sehttp240::win {

struct HttpResult { int status = 0; std::string body; std::string error; };

// Cooperative cancellation for one synchronous WinHTTP request (Sol review A06; re-review N01).
// cancel() only sets a flag. Handles are owned, used and closed exclusively by the worker thread
// (no cross-thread WinHttpCloseHandle, so a handle can never be used after close or after its
// value was reused). The worker checks the flag between every WinHTTP step and stops. A step that
// is already blocked finishes or times out (bounded by WinHttpSetTimeouts); its result is then
// discarded by the epoch check in HttpCore::complete, so it can never answer a newer conversation.
class CancelToken {
public:
	void cancel() noexcept { cancelled_.store(true, std::memory_order_release); }
	[[nodiscard]] bool cancelled() const noexcept { return cancelled_.load(std::memory_order_acquire); }
private:
	std::atomic<bool> cancelled_{ false };
};

// Synchronous POST to http://<host>:<port><path>; bounded body size; never throws.
// Redirects are never followed (Sol review A07): a 3xx is returned as an error, so a request can
// never leave the loopback authority it was validated for.
HttpResult post(const Request& r, std::size_t maxBody, CancelToken& cancel);

// Base address of the running executable (GetModuleHandleW(nullptr)); replaces the pre-libxse
// REL::Module::get().base() (Sol review A01). libxse's REL::ID resolves against the same module.
std::uintptr_t module_base();

// Directory of the running executable (UTF-8, trailing separator).
std::string exe_directory();

// Whole file read through the wide-char API (std::ifstream(std::string) would interpret the UTF-8
// path in the ANSI code page and fail for non-ASCII install directories). Empty on any error or if
// the file exceeds maxBytes.
std::vector<std::uint8_t> read_file(const std::string& utf8Path, std::size_t maxBytes);

// SizeOfImage of the module at `base` (0 if headers look wrong).
std::uint32_t image_size(std::uintptr_t base);

}  // namespace f4sehttp240::win
