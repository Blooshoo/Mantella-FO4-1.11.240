// Evaluates kHttpIds240 against an Address Library + PE image (on-disk file offline, or the loaded
// module in the plugin). Shared by tests/test_http_ids.cpp and plugin/F4SE_HTTP240.cpp.
#pragma once

#include "ids240.h"
#include "mantella240/guard.h"

#include <cstdio>
#include <cstring>
#include <optional>
#include <string>
#include <vector>

namespace f4sehttp240 {

struct IdResult {
	const IdSpec* spec = nullptr;
	std::optional<std::uint64_t> rva;
	bool ok = false;
	std::string detail;
};

struct IdReport {
	std::vector<IdResult> results;
	[[nodiscard]] bool all_required_ok() const {
		for (const auto& r : results) if (r.spec->required && !r.ok) return false;
		return !results.empty();
	}
};

// Code RVAs must be in an executable section and look like an MSVC function start (16-byte
// aligned, preceded by int3/ret padding). Data RVAs must be 8-aligned in a writable,
// non-executable section. Cross-checked entries must equal the F4SE 0.7.9 RVA.
inline IdResult check_id(const IdSpec& spec, const mantella240::AddressLibrary& lib, const mantella240::PeImage& pe) {
	IdResult r;
	r.spec = &spec;
	r.rva = lib.rva(spec.id);
	if (!r.rva) { r.detail = "ID missing from address library"; return r; }
	const auto* sec = pe.section_of(*r.rva, spec.kind == IdKind::Data ? 8 : 1);
	if (!sec) { r.detail = "RVA outside every section"; return r; }
	if (spec.kind == IdKind::Data) {
		if (!sec->writable() || sec->executable()) { r.detail = "data not in writable non-exec section (" + sec->name + ")"; return r; }
		if (*r.rva % 8) { r.detail = "data not 8-aligned"; return r; }
	} else {
		if (!sec->executable()) { r.detail = "code not in executable section (" + sec->name + ")"; return r; }
		if (*r.rva % 16) { r.detail = "code RVA not 16-aligned"; return r; }
		auto prev = pe.bytes(*r.rva - 1, 1);
		if (prev.size() == 1 && prev[0] != 0xCC && prev[0] != 0xC3 && prev[0] != 0x90) {
			r.detail = "byte before code RVA is not padding/ret"; return r;
		}
	}
	auto hex = [](std::uint64_t v) { char b[24]; std::snprintf(b, sizeof b, "0x%llX", static_cast<unsigned long long>(v)); return std::string(b); };
	if (spec.cross == CrossCheck::F4seLeaTarget) {
		// REX.W 8D /r with ModRM mod=00 rm=101 (RIP-relative): 48|4C 8D xx disp32, 7 bytes
		auto b = pe.bytes(*r.rva + spec.xref_off, 7);
		if (b.size() != 7 || (b[0] != 0x48 && b[0] != 0x4C) || b[1] != 0x8D || (b[2] & 0xC7) != 0x05) {
			r.detail = "expected rip-relative LEA at +" + hex(spec.xref_off); return r;
		}
		std::int32_t disp; std::memcpy(&disp, b.data() + 3, 4);
		const auto target = static_cast<std::uint64_t>(static_cast<std::int64_t>(*r.rva + spec.xref_off + 7) + disp);
		if (target != spec.f4se_rva) { r.detail = "LEA target " + hex(target) + " != F4SE 0.7.9 " + hex(spec.f4se_rva); return r; }
		r.ok = true;
		r.detail = "LEA at +" + hex(spec.xref_off) + " targets F4SE 0.7.9 " + hex(spec.f4se_rva);
		return r;
	}
	if (spec.cross == CrossCheck::F4seCallsIt) {
		auto b = pe.bytes(spec.f4se_rva + spec.xref_off, 5);
		if (b.size() != 5 || b[0] != 0xE8) { r.detail = "expected call rel32 in F4SE-named function at " + hex(spec.f4se_rva + spec.xref_off); return r; }
		std::int32_t disp; std::memcpy(&disp, b.data() + 1, 4);
		const auto target = static_cast<std::uint64_t>(static_cast<std::int64_t>(spec.f4se_rva + spec.xref_off + 5) + disp);
		if (target != *r.rva) { r.detail = "F4SE 0.7.9 function " + hex(spec.f4se_rva) + " calls " + hex(target) + ", not " + hex(*r.rva); return r; }
		r.ok = true;
		r.detail = "called by F4SE 0.7.9 function " + hex(spec.f4se_rva) + " at +" + hex(spec.xref_off);
		return r;
	}
	if (spec.cross != CrossCheck::None && *r.rva != spec.f4se_rva) {
		char buf[96];
		std::snprintf(buf, sizeof buf, "RVA 0x%llX != F4SE 0.7.9 0x%llX", static_cast<unsigned long long>(*r.rva),
			static_cast<unsigned long long>(spec.f4se_rva));
		r.detail = buf;
		return r;
	}
	r.ok = true;
	r.detail = spec.cross == CrossCheck::F4seEqual ? "matches F4SE 0.7.9" :
	           spec.cross == CrossCheck::F4seTentative ? "matches F4SE 0.7.9 (role match)" :
	           "section/alignment plausible (no second source)";
	return r;
}

inline IdReport evaluate_ids(const mantella240::AddressLibrary& lib, const mantella240::PeImage& pe) {
	IdReport rep;
	for (const auto& s : kHttpIds240) rep.results.push_back(check_id(s, lib, pe));
	return rep;
}

}  // namespace f4sehttp240
