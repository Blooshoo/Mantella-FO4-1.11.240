// Mapping verification for the F4SE_HTTP 1.11.240 port: libxse Address Library IDs vs the
// local Fallout4.exe and official F4SE 0.7.9 hardcoded RVAs. Reads files READ-ONLY.
// Env: MANTELLA240_EXE, MANTELLA240_ADDRLIB (skips the real-file part if unset).
#include "f4sehttp240/ids_check.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)

using namespace f4sehttp240;

static std::vector<std::uint8_t> slurp(const char* p) {
	std::ifstream f(p, std::ios::binary);
	return { std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>() };
}

int main() {
	// table sanity
	for (const auto& s : kHttpIds240) {
		CHECK(s.id != 0);
		CHECK((s.cross == CrossCheck::None) == (s.f4se_rva == 0));
		CHECK((s.cross == CrossCheck::F4seLeaTarget || s.cross == CrossCheck::F4seCallsIt) == (s.xref_off != 0));
		// a Data entry can never be checked by instruction cross-reference
		if (s.kind == IdKind::Data) CHECK(s.cross == CrossCheck::F4seEqual || s.cross == CrossCheck::None);
		for (auto legacy : kUpstreamLegacyIds) CHECK(s.id != legacy);
	}
	const char* exe = std::getenv("MANTELLA240_EXE");
	const char* al = std::getenv("MANTELLA240_ADDRLIB");
	if (!exe || !al) { std::printf("test_http_ids: table only (MANTELLA240_EXE/ADDRLIB unset), %d failures\n", failures); return failures ? 1 : 0; }

	auto exeBytes = slurp(exe), alBytes = slurp(al);
	std::string err;
	auto lib = mantella240::AddressLibrary::parse(alBytes, &err);
	auto pe = mantella240::PeImage::parse(exeBytes, true, &err);
	CHECK(lib.has_value());
	CHECK(pe.has_value());
	if (!lib || !pe) { std::fprintf(stderr, "parse error: %s\n", err.c_str()); return 1; }

	auto rep = evaluate_ids(*lib, *pe);
	int strict = 0, tentativeMismatch = 0;
	for (const auto& r : rep.results) {
		std::printf("  %-48s id %-8llu rva %-10s %s %s\n", std::string(r.spec->name).c_str(),
			static_cast<unsigned long long>(r.spec->id),
			r.rva ? ("0x" + [&] { char b[20]; std::snprintf(b, sizeof b, "%llX", static_cast<unsigned long long>(*r.rva)); return std::string(b); }()).c_str() : "-",
			r.ok ? "OK  " : "FAIL", r.detail.c_str());
		if (r.spec->cross == CrossCheck::F4seTentative && !r.ok) ++tentativeMismatch;
		else if (!r.ok) ++strict;
	}
	CHECK(strict == 0);
	for (auto legacy : kUpstreamLegacyIds) {
		auto rva = lib->rva(legacy);
		std::printf("  upstream legacy id %-8llu -> %s\n", static_cast<unsigned long long>(legacy),
			rva ? "present (meaning on 1.11.240 unknown; NOT used by the port)" : "absent from 1.11.240 database");
	}
	// negative control: a tampered library must fail the strict cross-check
	{
		std::vector<std::pair<std::uint64_t, std::uint64_t>> pairs;
		for (const auto& s : kHttpIds240) pairs.emplace_back(s.id, (lib->rva(s.id).value_or(0x1000)) + 0x10);
		std::sort(pairs.begin(), pairs.end());
		std::vector<std::uint8_t> b(8 + pairs.size() * 16);
		std::uint64_t n = pairs.size();
		std::memcpy(b.data(), &n, 8);
		for (std::size_t i = 0; i < pairs.size(); ++i) { std::memcpy(b.data() + 8 + i * 16, &pairs[i].first, 8); std::memcpy(b.data() + 16 + i * 16, &pairs[i].second, 8); }
		auto bad = mantella240::AddressLibrary::parse(b);
		CHECK(bad.has_value());
		if (bad) CHECK(!evaluate_ids(*bad, *pe).all_required_ok());
	}
	std::printf("test_http_ids: %zu ids, strict failures %d, tentative mismatches %d, all_required_ok=%d, %d check failures\n",
		rep.results.size(), strict, tentativeMismatch, rep.all_required_ok() ? 1 : 0, failures);
	return failures ? 1 : 0;
}
