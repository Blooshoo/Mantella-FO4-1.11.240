// Mantella native bridge — fail-closed address/signature guard for Fallout 4 1.11.240.
//
// Header-only, no Windows/CommonLib dependency, so the exact same code is
//   (a) unit-tested on Linux against the real local Fallout4.exe + Address Library, and
//   (b) compiled into the F4SE plugin, where it reads the *loaded* module image.
//
// Principle: a native that touches engine memory is registered ONLY if every check for
// every offset it depends on passes. A failed or unknown check disables that native
// (it then returns a documented error value to Papyrus); nothing is "best-effort hooked".
#pragma once

#include <cstdint>
#include <cstring>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace mantella240 {

// ---------------------------------------------------------------------------
// Address Library (Fallout 4 NG/AE "version-1-11-240-0.bin" layout):
//   uint64 count, then `count` pairs of { uint64 id, uint64 rva }.
class AddressLibrary {
public:
	static std::optional<AddressLibrary> parse(std::span<const std::uint8_t> bytes, std::string* err = nullptr) {
		if (bytes.size() < 8) { if (err) *err = "address library too small"; return std::nullopt; }
		std::uint64_t count = 0;
		std::memcpy(&count, bytes.data(), 8);
		if (count == 0 || count > (bytes.size() - 8) / 16 || 8 + count * 16 != bytes.size()) {
			if (err) *err = "address library size does not match count*16+8";
			return std::nullopt;
		}
		AddressLibrary lib;
		lib.pairs_.resize(static_cast<std::size_t>(count));
		std::memcpy(lib.pairs_.data(), bytes.data() + 8, static_cast<std::size_t>(count) * 16);
		for (std::size_t i = 1; i < lib.pairs_.size(); ++i) {
			if (lib.pairs_[i].id <= lib.pairs_[i - 1].id) { lib.sorted_ = false; break; }
		}
		return lib;
	}

	[[nodiscard]] std::optional<std::uint64_t> rva(std::uint64_t id) const {
		if (sorted_) {
			std::size_t lo = 0, hi = pairs_.size();
			while (lo < hi) {
				std::size_t mid = (lo + hi) / 2;
				if (pairs_[mid].id < id) lo = mid + 1; else hi = mid;
			}
			if (lo < pairs_.size() && pairs_[lo].id == id) return pairs_[lo].rva;
			return std::nullopt;
		}
		for (const auto& p : pairs_) if (p.id == id) return p.rva;
		return std::nullopt;
	}
	[[nodiscard]] std::size_t size() const { return pairs_.size(); }

private:
	struct Pair { std::uint64_t id; std::uint64_t rva; };
	static_assert(sizeof(Pair) == 16);
	std::vector<Pair> pairs_;
	bool sorted_ = true;
};

// ---------------------------------------------------------------------------
// Minimal PE section view. `image` is either the on-disk file (file_layout=true) or a
// loaded module image starting at its base (file_layout=false, RVA == offset).
struct Section {
	std::string name;
	std::uint32_t rva = 0, vsize = 0, raw_ptr = 0, raw_size = 0, characteristics = 0;
	[[nodiscard]] bool contains(std::uint64_t r, std::uint64_t len = 1) const {
		// subtraction form: `r + len` wraps for a tampered RVA near 2^64
		return r >= rva && len <= vsize && r - rva <= static_cast<std::uint64_t>(vsize) - len;
	}
	[[nodiscard]] bool executable() const { return characteristics & 0x20000000u; }
	[[nodiscard]] bool writable() const { return characteristics & 0x80000000u; }
};

class PeImage {
public:
	static std::optional<PeImage> parse(std::span<const std::uint8_t> image, bool file_layout, std::string* err = nullptr) {
		auto fail = [&](const char* m) { if (err) *err = m; return std::optional<PeImage>{}; };
		if (image.size() < 0x40 || image[0] != 'M' || image[1] != 'Z') return fail("missing MZ");
		// 64-bit arithmetic and subtraction-form bounds: a 32-bit `pe + 24` wraps for e_lfanew near
		// 0xFFFFFFFF and would pass the check, then memcmp/rd16 read far outside `image`.
		const std::uint64_t pe = rd32(image, 0x3C);
		if (pe > image.size() || image.size() - pe < 24 || std::memcmp(image.data() + pe, "PE\0\0", 4) != 0)
			return fail("missing PE header");
		std::uint16_t machine = rd16(image, pe + 4);
		if (machine != 0x8664) return fail("not x64");
		std::uint16_t nsec = rd16(image, pe + 6);
		std::uint16_t opt = rd16(image, pe + 20);
		const std::uint64_t sec = pe + 24 + opt;  // <= 2^32 + 2^16 + 24: no 64-bit overflow
		if (sec > image.size() || (image.size() - sec) / 40 < nsec) return fail("truncated section table");
		PeImage out;
		out.image_ = image;
		out.file_layout_ = file_layout;
		for (std::uint16_t i = 0; i < nsec; ++i) {
			const std::size_t o = static_cast<std::size_t>(sec) + std::size_t(i) * 40;
			Section s;
			s.name.assign(reinterpret_cast<const char*>(image.data() + o), strnlen(reinterpret_cast<const char*>(image.data() + o), 8));
			s.vsize = rd32(image, o + 8);
			s.rva = rd32(image, o + 12);
			s.raw_size = rd32(image, o + 16);
			s.raw_ptr = rd32(image, o + 20);
			s.characteristics = rd32(image, o + 36);
			out.sections_.push_back(std::move(s));
		}
		return out;
	}

	[[nodiscard]] const Section* section_of(std::uint64_t rva, std::uint64_t len = 1) const {
		for (const auto& s : sections_) if (s.contains(rva, len)) return &s;
		return nullptr;
	}

	// Bytes at an RVA, or empty if not backed by initialised data.
	[[nodiscard]] std::span<const std::uint8_t> bytes(std::uint64_t rva, std::size_t len) const {
		const Section* s = section_of(rva, len);
		if (!s) return {};
		std::uint64_t off;
		if (file_layout_) {
			std::uint64_t within = rva - s->rva;
			if (within + len > s->raw_size) return {};  // zero-fill tail: no file bytes
			off = s->raw_ptr + within;
		} else {
			off = rva;
		}
		if (off + len > image_.size()) return {};
		return image_.subspan(static_cast<std::size_t>(off), len);
	}
	[[nodiscard]] const std::vector<Section>& sections() const { return sections_; }

private:
	static std::uint16_t rd16(std::span<const std::uint8_t> b, std::size_t o) { std::uint16_t v; std::memcpy(&v, b.data() + o, 2); return v; }
	static std::uint32_t rd32(std::span<const std::uint8_t> b, std::size_t o) { std::uint32_t v; std::memcpy(&v, b.data() + o, 4); return v; }
	std::span<const std::uint8_t> image_;
	bool file_layout_ = true;
	std::vector<Section> sections_;
};

// ---------------------------------------------------------------------------
// Pattern: "48 89 5C 24 08 ?? ..." with ?? wildcards.
inline std::optional<std::vector<int>> parse_pattern(std::string_view p) {
	std::vector<int> out;
	std::size_t i = 0;
	auto hex = [](char c) -> int {
		if (c >= '0' && c <= '9') return c - '0';
		if (c >= 'a' && c <= 'f') return c - 'a' + 10;
		if (c >= 'A' && c <= 'F') return c - 'A' + 10;
		return -1;
	};
	while (i < p.size()) {
		if (p[i] == ' ') { ++i; continue; }
		if (i + 1 >= p.size()) return std::nullopt;
		if (p[i] == '?' && p[i + 1] == '?') { out.push_back(-1); i += 2; continue; }
		int hi = hex(p[i]), lo = hex(p[i + 1]);
		if (hi < 0 || lo < 0) return std::nullopt;
		out.push_back(hi * 16 + lo);
		i += 2;
	}
	return out;
}

inline bool match_pattern(std::span<const std::uint8_t> bytes, const std::vector<int>& pat) {
	if (bytes.size() < pat.size()) return false;
	for (std::size_t i = 0; i < pat.size(); ++i)
		if (pat[i] >= 0 && bytes[i] != static_cast<std::uint8_t>(pat[i])) return false;
	return true;
}

}  // namespace mantella240
