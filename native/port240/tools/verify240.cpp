// Offline verifier: reads Fallout4.exe + version-1-11-240-0.bin (read-only) and prints which
// TopicInfoPatcher natives the fail-closed guard would enable. Exit 0 = all Verified offsets pass.
#include "mantella240/verify.h"

#include <cstdio>
#include <fstream>
#include <iterator>

static std::vector<std::uint8_t> slurp(const char* path) {
	std::ifstream f(path, std::ios::binary);
	return { std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>() };
}

int main(int argc, char** argv) {
	if (argc != 3) { std::fprintf(stderr, "usage: verify240 <Fallout4.exe> <version-1-11-240-0.bin>\n"); return 2; }
	auto exe = slurp(argv[1]);
	auto al = slurp(argv[2]);
	std::string err;
	auto pe = mantella240::PeImage::parse(exe, true, &err);
	if (!pe) { std::fprintf(stderr, "PE: %s\n", err.c_str()); return 2; }
	auto lib = mantella240::AddressLibrary::parse(al, &err);
	if (!lib) { std::fprintf(stderr, "AddressLibrary: %s\n", err.c_str()); return 2; }
	auto rep = mantella240::evaluate(*lib, *pe, false);
	int bad = 0;
	std::printf("address library entries: %zu\n\n", lib->size());
	for (const auto& o : rep.offsets) {
		const char* st = o.spec->status == mantella240::Status::Verified ? "VERIFIED-SPEC" :
			o.spec->status == mantella240::Status::LocatedAbiUnproven ? "ABI-UNPROVEN" : "UNRESOLVED";
		if (o.spec->status == mantella240::Status::Verified && !o.usable) ++bad;
		std::printf("%-40s id=%-8llu rva=%-10s %-13s usable=%d  %s\n", std::string(o.spec->name).c_str(),
			(unsigned long long)o.spec->id, o.rva ? ("0x" + [&] { char b[20]; std::snprintf(b, 20, "%llX", (unsigned long long)*o.rva); return std::string(b); }()).c_str() : "-",
			st, o.usable, o.detail.c_str());
	}
	std::printf("\n");
	for (const auto& n : rep.natives)
		std::printf("native %-24s %s  %s\n", std::string(n.spec->name).c_str(), n.enabled ? "ENABLED " : "DISABLED", n.enabled ? "" : std::string(n.spec->when_disabled).c_str());
	return bad ? 1 : 0;
}
