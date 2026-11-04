// Plugin-side glue: run the fail-closed guard against the LOADED Fallout4.exe image.
// STATIC SOURCE ONLY: not compiled in this milestone (no MSVC/clang-cl toolchain on BlooDesk).
#pragma once

#include "mantella240/verify.h"

#include <filesystem>
#include <fstream>
#include <iterator>

namespace mantella240::plugin {

struct GuardState {
	bool evaluated = false;
	bool runtime_is_240 = false;
	bool core_ok = false;  // every Status::Verified offset passed against the loaded image
	Report report;
};

inline GuardState& state() { static GuardState s; return s; }

// `base` = module base of Fallout4.exe, `addrlib_path` = Data/F4SE/Plugins/version-1-11-240-0.bin.
// Returns false (and leaves every engine-touching native disabled) on any failure.
template <class LogFn>
bool evaluate_loaded(std::uintptr_t base, const std::filesystem::path& addrlib_path, LogFn&& log) {
	auto& st = state();
	st.evaluated = true;
	auto* p = reinterpret_cast<const std::uint8_t*>(base);
	std::uint32_t pe_off; std::memcpy(&pe_off, p + 0x3C, 4);
	std::uint32_t size_of_image; std::memcpy(&size_of_image, p + pe_off + 24 + 56, 4);  // OptionalHeader64.SizeOfImage
	std::string err;
	auto pe = PeImage::parse(std::span<const std::uint8_t>(p, size_of_image), /*file_layout=*/false, &err);
	if (!pe) { log("guard: PE parse failed: " + err); return false; }
	std::ifstream f(addrlib_path, std::ios::binary);
	std::vector<std::uint8_t> bytes{ std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>() };
	auto lib = AddressLibrary::parse(bytes, &err);
	if (!lib) { log("guard: address library: " + err); return false; }
	st.report = evaluate(*lib, *pe, /*loaded_image=*/true);
	st.core_ok = true;
	for (const auto& o : st.report.offsets) {
		log("guard: " + std::string(o.spec->name) + ": " + (o.usable ? "OK " : "OFF ") + o.detail);
		if (o.spec->status == Status::Verified && !o.usable) st.core_ok = false;
	}
	for (const auto& n : st.report.natives)
		log("guard: native " + std::string(n.spec->name) + (n.enabled ? " enabled" : " DISABLED: " + n.detail));
	return st.core_ok;
}

// A native may touch engine memory only if the guard ran, core offsets passed and its own deps passed.
inline bool allowed(std::string_view native) {
	const auto& st = state();
	return st.evaluated && st.runtime_is_240 && st.core_ok && st.report.native_enabled(native);
}

}  // namespace mantella240::plugin
