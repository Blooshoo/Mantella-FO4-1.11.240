// Engine addresses reached by the F4SE_HTTP 1.11.240 port, with independent cross-checks.
//
// The plugin is built on libxse/commonlibf4 7c8c6f8 (RUNTIME_LATEST = 1.11.240). Every engine
// function/global it reaches through that library is listed here with its Address Library ID.
// Where the official F4SE 0.7.9 source (6f6a7ca, which ships for runtime 1.11.240 and hardcodes
// RVAs) names the same function/global, its RVA is recorded as an INDEPENDENT second source; the
// test requires the two to agree. Entries without a second source are checked for section kind
// and plausibility only and are labelled accordingly — they are NOT claimed verified.
//
// What the plugin actually enforces at load (ids_check.h): every required ID must resolve, sit in
// the right kind of section and look like a function start / aligned data slot; entries with a
// second source must additionally match it (equal RVA, or an instruction-level cross-reference to
// the F4SE-named entity). Entries with CrossCheck::None get the plausibility checks ONLY - they are
// not pinned to an expected RVA. Offline results (test_http_ids against the local exe sha256
// fdcef37a... and Address Library sha256 65985cc2...) are not a substitute for a runtime check.
//
// Deliberately NOT used by the port (upstream cdff839 FunctionArgs.h relied on them; both are
// pre-NG IDs): REL::ID(69733) "CreateThreadScrapFunction", REL::ID(445184) "ArrayWrapper::ReplaceArray".
#pragma once

#include <array>
#include <cstdint>
#include <string_view>

namespace f4sehttp240 {

enum class IdKind : std::uint8_t { Code, Data };

enum class CrossCheck : std::uint8_t {
	F4seEqual,     // F4SE 0.7.9 hardcodes the same entity; RVAs must be identical
	F4seTentative, // F4SE has a function with a matching role; equality expected, mismatch -> investigate (not fatal offline)
	F4seLeaTarget, // the resolved function contains `lea reg,[rip+X]` at xref_off with X == f4se_rva
	F4seCallsIt,   // the F4SE-named function at f4se_rva contains `call rel32` at xref_off targeting the resolved RVA
	None,          // no second source; section/plausibility checks only
};

struct IdSpec {
	std::string_view name;
	std::uint64_t id;
	IdKind kind;
	CrossCheck cross;
	std::uint64_t f4se_rva;  // 0 when no second source
	bool required;           // plugin refuses to load if this fails at runtime
	std::string_view used_for;
	std::uint32_t xref_off = 0;  // F4seLeaTarget / F4seCallsIt: instruction offset inside the function
};

inline constexpr std::array kHttpIds240 = {
	IdSpec{ "GameVM::Singleton", 4796420, IdKind::Data, CrossCheck::F4seEqual, 0x30EB308, true,
		"libxse GameVM::GetSingleton (type info lookups while packing returns); F4SE g_gameVM" },
	// libxse MemoryManager::GetSingleton() CALLS this ID (REL::Relocation<func_t>); it is a getter
	// function, not the heap object. Its body (`lea rbx,[rip+...]` at +0x0F and `lea rax,[rip+...]`
	// at +0x2E) returns F4SE 0.7.9's g_mainHeap 0x3E74AB0 - the cross-check used here.
	IdSpec{ "MemoryManager::GetSingleton", 4471522, IdKind::Code, CrossCheck::F4seLeaTarget, 0x3E74AB0, true,
		"heap for BSTArray/Variable/NativeFunction allocations; returns F4SE g_mainHeap", 0x0F },
	IdSpec{ "MemoryManager::Allocate", 2267872, IdKind::Code, CrossCheck::F4seEqual, 0x1657DE0, true,
		"libxse F4_HEAP_REDEFINE_NEW / BSTArray growth; F4SE Heap::Allocate" },
	IdSpec{ "MemoryManager::Deallocate", 2267874, IdKind::Code, CrossCheck::F4seEqual, 0x1658240, true,
		"matching free; F4SE Heap::Free" },
	IdSpec{ "BSScript::NF_util::NativeFunctionBase::Call", 2314780, IdKind::Code, CrossCheck::F4seTentative, 0x21012A0, true,
		"IFunction::Call for every bound native; F4SE NativeFunctionBase::Impl_Invoke" },
	IdSpec{ "BSScript::Stack::GetPageForFrame", 2314680, IdKind::Code, CrossCheck::None, 0, true,
		"argument unmarshalling (libxse DispatchHelper)" },
	IdSpec{ "BSScript::Stack::GetStackFrameVariable", 2314681, IdKind::Code, CrossCheck::None, 0, true,
		"argument unmarshalling (libxse DispatchHelper)" },
	// F4SE 0.7.9 StringCache::Ref::ctor (0x167C1E0) is `GetEntry(this, buf, false)`: call rel32 at +0x18.
	IdSpec{ "BSStringPool::GetEntry<char>", 2268729, IdKind::Code, CrossCheck::F4seCallsIt, 0x167C1E0, true,
		"BSFixedString construction for string returns / string array elements; called by F4SE StringCache::Ref::ctor", 0x18 },
	IdSpec{ "BSStringPool::Entry::Release", 2268720, IdKind::Code, CrossCheck::F4seTentative, 0x167D4A0, true,
		"BSFixedString release; F4SE StringCache::Ref::Release (role match only)" },
};

// Upstream-only IDs, reported for the record (the port must not reference them).
inline constexpr std::array<std::uint64_t, 2> kUpstreamLegacyIds = { 69733, 445184 };

}  // namespace f4sehttp240
