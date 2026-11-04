// Evaluates kOffsets240 / kNatives240 against an Address Library + PE image (file or loaded module).
#pragma once

#include "guard.h"
#include "offsets240.h"

#include <map>
#include <string>
#include <vector>

namespace mantella240 {

struct OffsetResult {
	const OffsetSpec* spec = nullptr;
	std::optional<std::uint64_t> rva;
	bool usable = false;  // may be used at runtime
	std::string detail;
};

struct NativeResult {
	const NativeSpec* spec = nullptr;
	bool enabled = false;
	std::string detail;
};

struct Report {
	std::vector<OffsetResult> offsets;
	std::vector<NativeResult> natives;
	[[nodiscard]] const OffsetResult* find(std::string_view name) const {
		for (const auto& r : offsets) if (r.spec->name == name) return &r;
		return nullptr;
	}
	[[nodiscard]] bool native_enabled(std::string_view name) const {
		for (const auto& n : natives) if (n.spec->name == name) return n.enabled;
		return false;
	}
};

inline OffsetResult check_offset(const OffsetSpec& spec, const AddressLibrary& lib, const PeImage& pe, bool loaded_image) {
	OffsetResult r;
	r.spec = &spec;
	if (spec.id) r.rva = lib.rva(spec.id);
	if (spec.status == Status::Unresolved) {
		r.detail = spec.id && r.rva ? "unresolved semantics (ID present but not trusted)" : "unresolved: no trusted .240 target";
		return r;
	}
	if (!r.rva) { r.detail = "ID missing from address library"; return r; }
	const Section* sec = pe.section_of(*r.rva, spec.kind == Kind::DataSlot ? 8 : 1);
	if (!sec) { r.detail = "RVA outside every section"; return r; }

	if (spec.kind == Kind::DataSlot) {
		if (!sec->writable() || sec->executable()) { r.detail = "data slot not in a writable, non-executable section (" + sec->name + ")"; return r; }
		if (*r.rva % 8 != 0) { r.detail = "data slot not 8-byte aligned"; return r; }
		r.usable = spec.status == Status::Verified;
		r.detail = "data slot in " + sec->name + (loaded_image ? "" : " (offline: pointer value only known at runtime)");
		return r;
	}

	// Function
	if (!sec->executable()) { r.detail = "function RVA not in an executable section"; return r; }
	auto pat = parse_pattern(spec.pattern);
	if (!pat || pat->empty()) { r.detail = "no signature pinned"; return r; }
	auto bytes = pe.bytes(*r.rva, pat->size());
	if (bytes.empty() || !match_pattern(bytes, *pat)) { r.detail = "signature mismatch at RVA"; return r; }
	for (std::uint8_t i = 0; i < spec.nrefs; ++i) {
		const RipRef& ref = spec.refs[i];
		auto disp_bytes = pe.bytes(*r.rva + ref.insn_offset + ref.disp_offset, 4);
		if (disp_bytes.size() != 4) { r.detail = "cannot read rip-relative operand"; return r; }
		std::int32_t disp;
		std::memcpy(&disp, disp_bytes.data(), 4);
		std::int64_t target = static_cast<std::int64_t>(*r.rva + ref.insn_offset + ref.insn_len) + disp;
		auto want = lib.rva(ref.target_id);
		if (!want || static_cast<std::uint64_t>(target) != *want) {
			r.detail = "rip-relative reference does not resolve to ID " + std::to_string(ref.target_id);
			return r;
		}
	}
	if (spec.status == Status::LocatedAbiUnproven) {
		r.detail = "signature matches, but ABI unproven -> not enabled";
		return r;
	}
	r.usable = true;
	r.detail = "signature + " + std::to_string(spec.nrefs) + " cross-reference(s) verified";
	return r;
}

// Derives every native's enabled flag from the offsets already in `rep` (split out of evaluate() so the
// plugin-gate tests can build guard states without duplicating this rule).
inline void evaluate_natives(Report& rep) {
	rep.natives.clear();
	for (const auto& n : kNatives240) {
		NativeResult nr;
		nr.spec = &n;
		nr.enabled = true;
		for (std::uint8_t i = 0; i < n.nneeds; ++i) {
			const OffsetResult* o = rep.find(n.needs[i]);
			if (!o || !o->usable) {
				nr.enabled = false;
				nr.detail = "needs " + std::string(n.needs[i]) + ": " + (o ? o->detail : "unknown offset");
				break;
			}
		}
		if (nr.enabled) nr.detail = "all dependencies verified";
		rep.natives.push_back(std::move(nr));
	}
}

inline Report evaluate(const AddressLibrary& lib, const PeImage& pe, bool loaded_image) {
	Report rep;
	for (const auto& spec : kOffsets240) rep.offsets.push_back(check_offset(spec, lib, pe, loaded_image));
	evaluate_natives(rep);
	return rep;
}

}  // namespace mantella240
