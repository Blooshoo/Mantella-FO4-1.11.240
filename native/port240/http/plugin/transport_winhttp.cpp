// WinHTTP transport for F4SE_HTTP240 (static source only; see F4SE_HTTP240.cpp header).
#include "transport.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <winhttp.h>

#include <algorithm>
#include <iterator>
#include <memory>

namespace f4sehttp240::win {

namespace {

std::wstring widen(const std::string& s) {
	if (s.empty()) return {};
	int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
	std::wstring w(static_cast<std::size_t>(n > 0 ? n : 0), L'\0');
	if (n > 0) MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), n);
	return w;
}

struct HClose { void operator()(HINTERNET h) const { if (h) WinHttpCloseHandle(h); } };
using Handle = std::unique_ptr<void, HClose>;

// Per-operation inactivity bound. Mantella's server can take a while for the first LLM tokens; the
// scripts' own poll timeout (HTTPTimeOutHolotapeValue polls) ends the conversation earlier, and the
// next conversation start / clearAllDictionaries cancels anything still blocked (CancelToken).
constexpr int kReceiveTimeoutMs = 3 * 60 * 1000;

}  // namespace

std::uintptr_t module_base() { return reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr)); }

HttpResult post(const Request& r, std::size_t maxBody, CancelToken& cancel) {
	HttpResult out;
	auto fail = [&](const char* what) {
		const DWORD e = GetLastError();
		out.status = 0;
		out.body.clear();
		out.error = cancel.cancelled() ? std::string("request cancelled (conversation ended)")
		                               : std::string(what) + " (WinHTTP error " + std::to_string(e) + ")";
		return out;
	};
	Handle session(WinHttpOpen(L"F4SE_HTTP240", WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0));
	if (!session) return fail("WinHttpOpen failed");
	// resolve, connect, send, receive. connect 0 -> 60 s (upstream: cpr ConnectTimeout{timeout} ms)
	if (!WinHttpSetTimeouts(session.get(), 0, r.connect_timeout_ms > 0 ? r.connect_timeout_ms : 60000, 30000, kReceiveTimeoutMs))
		return fail("WinHttpSetTimeouts failed");
	// Never follow redirects (A07): the authority was validated as loopback; a 3xx Location may not be.
	DWORD never = WINHTTP_OPTION_REDIRECT_POLICY_NEVER;
	if (!WinHttpSetOption(session.get(), WINHTTP_OPTION_REDIRECT_POLICY, &never, sizeof never))
		return fail("cannot disable redirects");
	auto stop = [&] { return cancel.cancelled(); };
	if (stop()) return fail("cancelled");
	Handle conn(WinHttpConnect(session.get(), widen(r.host).c_str(), static_cast<INTERNET_PORT>(r.port), 0));
	if (!conn) return fail("WinHttpConnect failed");
	// Declared after `conn`, so it is closed first; all three handles are closed by this thread only.
	Handle reqH(WinHttpOpenRequest(conn.get(), L"POST", widen(r.path).c_str(), nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0));
	if (!reqH) return fail("WinHttpOpenRequest failed");
	HINTERNET req = reqH.get();
	DWORD noRedirects = WINHTTP_DISABLE_REDIRECTS;
	if (!WinHttpSetOption(req, WINHTTP_OPTION_DISABLE_FEATURE, &noRedirects, sizeof noRedirects))
		return fail("cannot disable redirects");
	if (stop()) return fail("cancelled");
	// Upstream also sent Basic "user:pass"; Mantella's server does not use it, so it is not sent.
	const wchar_t* headers = L"Content-Type: application/json\r\nAccept: application/json\r\n";
	if (!WinHttpSendRequest(req, headers, static_cast<DWORD>(-1L), const_cast<char*>(r.body.data()),
			static_cast<DWORD>(r.body.size()), static_cast<DWORD>(r.body.size()), 0))
		return fail("request failed");
	if (stop()) return fail("cancelled");
	if (!WinHttpReceiveResponse(req, nullptr)) return fail("no response");
	if (stop()) return fail("cancelled");
	DWORD status = 0, len = sizeof(status);
	if (!WinHttpQueryHeaders(req, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &status, &len, WINHTTP_NO_HEADER_INDEX))
		return fail("no HTTP status");
	if (status >= 300 && status < 400) {  // belt and braces: redirects are disabled above
		out.status = 0;
		out.error = "redirect refused (HTTP " + std::to_string(status) + ")";
		return out;
	}
	out.status = static_cast<int>(status);
	for (;;) {
		if (stop()) { out.status = 0; out.body.clear(); out.error = "request cancelled (conversation ended)"; break; }
		DWORD avail = 0;
		if (!WinHttpQueryDataAvailable(req, &avail)) {  // e.g. receive timeout mid-body: never hand a truncated body on
			std::string why = cancel.cancelled() ? std::string("request cancelled (conversation ended)")
			                                     : "reply body interrupted (WinHTTP error " + std::to_string(GetLastError()) + ")";
			out.status = 0; out.body.clear(); out.error = std::move(why);
			break;
		}
		if (avail == 0) break;
		if (out.body.size() + avail > maxBody) { out.status = 0; out.error = "reply exceeds size limit"; out.body.clear(); break; }
		std::string chunk(avail, '\0');
		DWORD read = 0;
		if (!WinHttpReadData(req, chunk.data(), avail, &read)) { out.status = 0; out.error = "read failed"; out.body.clear(); break; }
		out.body.append(chunk.data(), read);
	}
	return out;
}

std::string exe_directory() {
	wchar_t buf[MAX_PATH * 2]{};
	DWORD n = GetModuleFileNameW(nullptr, buf, static_cast<DWORD>(std::size(buf)));
	std::wstring w(buf, n);
	auto slash = w.find_last_of(L"\\/");
	w = slash == std::wstring::npos ? L"" : w.substr(0, slash + 1);
	int m = WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
	std::string s(static_cast<std::size_t>(m > 0 ? m : 0), '\0');
	if (m > 0) WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), s.data(), m, nullptr, nullptr);
	return s;
}

std::vector<std::uint8_t> read_file(const std::string& utf8Path, std::size_t maxBytes) {
	std::vector<std::uint8_t> out;
	HANDLE f = CreateFileW(widen(utf8Path).c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
	if (f == INVALID_HANDLE_VALUE) return out;
	LARGE_INTEGER size{};
	if (GetFileSizeEx(f, &size) && size.QuadPart > 0 && static_cast<unsigned long long>(size.QuadPart) <= maxBytes) {
		out.resize(static_cast<std::size_t>(size.QuadPart));
		std::size_t done = 0;
		while (done < out.size()) {
			DWORD chunk = static_cast<DWORD>(std::min<std::size_t>(out.size() - done, 1u << 30));
			DWORD got = 0;
			if (!ReadFile(f, out.data() + done, chunk, &got, nullptr) || got == 0) { out.clear(); break; }
			done += got;
		}
	}
	CloseHandle(f);
	return out;
}

std::uint32_t image_size(std::uintptr_t base) {
	const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
	// the loader maps the headers in the first page; never chase e_lfanew outside it
	if (!base || dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 0 ||
	    static_cast<std::uint32_t>(dos->e_lfanew) > 0x1000 - sizeof(IMAGE_NT_HEADERS64)) return 0;
	const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + static_cast<std::uintptr_t>(dos->e_lfanew));
	if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return 0;
	return nt->OptionalHeader.SizeOfImage;
}

}  // namespace f4sehttp240::win
