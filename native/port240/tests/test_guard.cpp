// Unit tests for the fail-closed guard. Synthetic tests always run; real-file tests run when
// MANTELLA240_EXE and MANTELLA240_ADDRLIB point at the local (read-only) files.
#include "mantella240/verify.h"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)

using namespace mantella240;

static std::vector<std::uint8_t> slurp(const char* p) {
	std::ifstream f(p, std::ios::binary);
	return { std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>() };
}

static std::vector<std::uint8_t> make_addrlib(std::vector<std::pair<std::uint64_t, std::uint64_t>> pairs) {
	std::vector<std::uint8_t> b(8 + pairs.size() * 16);
	std::uint64_t n = pairs.size();
	std::memcpy(b.data(), &n, 8);
	for (std::size_t i = 0; i < pairs.size(); ++i) {
		std::memcpy(b.data() + 8 + i * 16, &pairs[i].first, 8);
		std::memcpy(b.data() + 16 + i * 16, &pairs[i].second, 8);
	}
	return b;
}

static void test_addrlib_parse() {
	auto good = make_addrlib({ { 1, 0x10 }, { 5, 0x50 }, { 9, 0x90 } });
	auto lib = AddressLibrary::parse(good);
	CHECK(lib && lib->size() == 3);
	CHECK(lib->rva(5) == 0x50u);
	CHECK(!lib->rva(4));
	auto truncated = good; truncated.pop_back();
	CHECK(!AddressLibrary::parse(truncated));
	std::vector<std::uint8_t> lying(8, 0); lying[0] = 0xFF;  // count 255, no payload
	CHECK(!AddressLibrary::parse(lying));
}

static void test_pattern() {
	auto p = parse_pattern("48 8B ?? 05");
	CHECK(p && p->size() == 4 && (*p)[2] == -1);
	std::uint8_t ok[] = { 0x48, 0x8B, 0x77, 0x05 };
	std::uint8_t bad[] = { 0x48, 0x8C, 0x77, 0x05 };
	CHECK(match_pattern(ok, *p));
	CHECK(!match_pattern(bad, *p));
	CHECK(!match_pattern(std::span<const std::uint8_t>(ok, 3), *p));
	CHECK(!parse_pattern("4"));
	CHECK(!parse_pattern("ZZ"));
}

static void test_pe_rejects_garbage() {
	std::vector<std::uint8_t> junk(512, 0);
	CHECK(!PeImage::parse(junk, true));
}

// Sol review C01: e_lfanew = 0xFFFFFFF0 made the old 32-bit `pe + 24` wrap to 8 and pass the bound
// check, followed by an out-of-bounds memcmp. Must fail cleanly (run under ASan by run_tests.sh).
static void test_pe_lfanew_wrap() {
	std::vector<std::uint8_t> small(64, 0);
	small[0] = 'M'; small[1] = 'Z';
	const std::uint32_t evil = 0xFFFFFFF0u;
	std::memcpy(small.data() + 0x3C, &evil, 4);
	CHECK(!PeImage::parse(small, true));
	const std::uint32_t edge = 64 - 23;  // PE signature fits, 24-byte file header does not
	std::memcpy(small.data() + 0x3C, &edge, 4);
	CHECK(!PeImage::parse(small, true));
	// huge section count must not overflow the table-size check
	std::vector<std::uint8_t> hdr(0x200, 0);
	hdr[0] = 'M'; hdr[1] = 'Z';
	const std::uint32_t pe = 0x40;
	std::memcpy(hdr.data() + 0x3C, &pe, 4);
	std::memcpy(hdr.data() + pe, "PE\0\0", 4);
	const std::uint16_t machine = 0x8664, nsec = 0xFFFF, opt = 0xFFFF;
	std::memcpy(hdr.data() + pe + 4, &machine, 2);
	std::memcpy(hdr.data() + pe + 6, &nsec, 2);
	std::memcpy(hdr.data() + pe + 20, &opt, 2);
	CHECK(!PeImage::parse(hdr, true));
}

// Section::contains must not wrap for a tampered RVA near 2^64.
static void test_section_contains_wrap() {
	Section s;
	s.rva = 0x1000; s.vsize = 0x1000;
	CHECK(s.contains(0x1000, 8));
	CHECK(s.contains(0x1FF8, 8));
	CHECK(!s.contains(0x1FF9, 8));
	CHECK(!s.contains(~std::uint64_t{0} - 3, 8));
	CHECK(!s.contains(0x1000, 0x2000));
}

static void test_real_files() {
	const char* exe_p = std::getenv("MANTELLA240_EXE");
	const char* al_p = std::getenv("MANTELLA240_ADDRLIB");
	if (!exe_p || !al_p) { std::printf("SKIP real-file tests (set MANTELLA240_EXE / MANTELLA240_ADDRLIB)\n"); return; }
	auto exe = slurp(exe_p);
	auto al = slurp(al_p);
	auto pe = PeImage::parse(exe, true);
	auto lib = AddressLibrary::parse(al);
	CHECK(pe.has_value());
	CHECK(lib.has_value());
	if (!pe || !lib) return;
	CHECK(lib->size() == 652306);
	CHECK(lib->rva(kID_TESForm_GetFormByNumericID) == 0x311B80u);
	CHECK(lib->rva(kID_UI_Singleton) == 0x30E8930u);
	CHECK(lib->rva(kID_GameSettingCollection_Singleton) == 0x30F8920u);
	CHECK(lib->rva(kID_PlayerCharacter_Singleton) == 0x31EDE50u);
	CHECK(!lib->rva(kID_Legacy_AudioClearCache));

	auto rep = evaluate(*lib, *pe, false);
	for (const auto& o : rep.offsets)
		if (o.spec->status == Status::Verified) { if (!o.usable) std::fprintf(stderr, "  %s: %s\n", o.spec->name.data(), o.detail.c_str()); CHECK(o.usable); }
		else CHECK(!o.usable);
	CHECK(rep.native_enabled("isMenuModeActive"));
	CHECK(rep.native_enabled("saveFloat"));
	CHECK(rep.native_enabled("PatchTopicInfo"));
	CHECK(!rep.native_enabled("SetOverrideFileName"));
	CHECK(!rep.native_enabled("ClearCache"));
	CHECK(!rep.native_enabled("TakeScreenShot"));
	CHECK(!rep.native_enabled("GetLastActorCoords"));

	// Negative: the .191 hardcoded lookup RVA 0x311850 must NOT pass the pinned lookup signature.
	auto pat = parse_pattern(kOffsets240[0].pattern);
	CHECK(pat && !match_pattern(pe->bytes(0x311850, pat->size()), *pat));

	// Negative: a corrupted/mismatched address library (lookup ID pointing 0x10 bytes off) fails closed.
	std::vector<std::uint8_t> tampered = al;
	std::uint64_t n; std::memcpy(&n, tampered.data(), 8);
	for (std::uint64_t i = 0; i < n; ++i) {
		std::uint64_t id; std::memcpy(&id, tampered.data() + 8 + i * 16, 8);
		if (id == kID_TESForm_GetFormByNumericID) { std::uint64_t r = 0x311B90; std::memcpy(tampered.data() + 16 + i * 16, &r, 8); }
		if (id == kID_TESForm_AllForms) { std::uint64_t r = 0x30EBED0; std::memcpy(tampered.data() + 16 + i * 16, &r, 8); }
	}
	auto tlib = AddressLibrary::parse(tampered);
	CHECK(tlib.has_value());
	if (tlib) {
		auto trep = evaluate(*tlib, *pe, false);
		CHECK(!trep.find("TESForm::GetFormByNumericID")->usable);
	}
	std::printf("real-file tests ran against %zu-entry address library\n", lib->size());
}

int main() {
	test_pe_lfanew_wrap();
	test_section_contains_wrap();
	test_addrlib_parse();
	test_pattern();
	test_pe_rejects_garbage();
	test_real_files();
	if (failures) { std::fprintf(stderr, "%d failure(s)\n", failures); return 1; }
	std::printf("all guard tests passed\n");
	return 0;
}
