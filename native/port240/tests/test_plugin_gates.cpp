// Re-review G01: TopicInfoPatcher's GameDataReady listener used to call
// RE::GameSettingCollection::GetSingleton() unconditionally, even after the guard failed. The production
// decision now lives in plugin/Guard240.h (acquire_game_settings, called verbatim by MessageHandler); this
// test drives that exact function with a counting fake accessor - no engine singleton is ever touched -
// through every guard state, plus the public native refusals of the same states.
#include "Guard240.h"

#include <cstdio>
#include <array>
#include <cstring>
#include <optional>
#include <string_view>

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)

using namespace mantella240;
using mantella240::plugin::state;

struct FakeSettings {};
static FakeSettings g_fake;
static int g_calls = 0;
static FakeSettings* fake_accessor() { ++g_calls; return &g_fake; }

// Guard state as evaluate_loaded would leave it. `usable(spec)` decides each offset's result; natives are
// derived by the production rule (evaluate_natives).
template <class Usable>
static void set_state(bool evaluated, bool runtime_240, bool core_ok, Usable&& usable, std::string_view drop = {}) {
	plugin::GuardState st;
	st.evaluated = evaluated;
	st.runtime_is_240 = runtime_240;
	st.core_ok = core_ok;
	for (const auto& spec : kOffsets240) {
		if (spec.name == drop) continue;  // offset missing from the report
		OffsetResult r;
		r.spec = &spec;
		r.usable = usable(spec);
		r.detail = r.usable ? "test: usable" : "test: not usable";
		st.report.offsets.push_back(r);
	}
	evaluate_natives(st.report);
	state() = st;
}
static auto as_catalog = [](const OffsetSpec& s) { return s.status == Status::Verified; };  // a passing image

// One GameDataReady dispatch through the production decision; returns what MessageHandler stores.
static FakeSettings* dispatch() { return plugin::acquire_game_settings(fake_accessor); }

// `all_natives`: the whole guard failed, so every engine native must be refused; otherwise only the
// natives that need the game-settings singleton.
static void expect_refused(const char* what, bool all_natives) {
	g_calls = 0;
	FakeSettings* got = dispatch();
	if (g_calls != 0 || got != nullptr) std::fprintf(stderr, "  state: %s\n", what);
	CHECK(g_calls == 0);   // the engine accessor was never invoked
	CHECK(got == nullptr); // settings unavailable
	for (std::string_view n : { "saveFloat", "saveInt", "restoreFloat", "restoreInt" }) CHECK(!plugin::allowed(n));
	if (all_natives)
		for (std::string_view n : { "isMenuModeActive", "PatchTopicInfo", "StringRemoveWhiteSpace" }) CHECK(!plugin::allowed(n));
}

int main() {
	// 1. never evaluated (guard not run yet, e.g. a message before Load finished)
	state() = plugin::GuardState{};
	expect_refused("never evaluated", true);

	// 2. address-library / PE validation failed: evaluate_loaded returns early, core_ok false, empty report
	set_state(true, true, false, [](const OffsetSpec&) { return false; });
	state().report.offsets.clear();
	evaluate_natives(state().report);
	expect_refused("library validation failed", true);

	// 3. core validation failed (one Verified offset did not match the loaded image)
	set_state(true, true, false, [](const OffsetSpec& s) { return s.status == Status::Verified && s.name != "UI::Singleton"; });
	expect_refused("core validation failed", true);

	// 4. wrong runtime, even with an otherwise passing report
	set_state(true, false, true, as_catalog);
	expect_refused("wrong runtime", true);

	// 5. singleton dependency missing from the report while everything else claims success
	set_state(true, true, true, as_catalog, plugin::kGameSettingsSingleton);
	expect_refused("singleton dependency missing", false);
	CHECK(plugin::allowed("PatchTopicInfo"));  // unrelated natives are not collateral damage

	// 6. singleton dependency present but not usable while core_ok claims success
	set_state(true, true, true, [](const OffsetSpec& s) { return s.status == Status::Verified && s.name != plugin::kGameSettingsSingleton; });
	expect_refused("singleton dependency not usable", false);

	// 7. fully validated: exactly one accessor call per dispatch, natives enabled as the catalog says
	set_state(true, true, true, as_catalog);
	g_calls = 0;
	CHECK(dispatch() == &g_fake);
	CHECK(g_calls == 1);
	for (std::string_view n : { "saveFloat", "saveInt", "restoreFloat", "restoreInt", "isMenuModeActive", "PatchTopicInfo", "StringRemoveWhiteSpace" })
		CHECK(plugin::allowed(n));
	for (std::string_view n : { "ClearCache", "SetOverrideFileName", "TakeScreenShot", "GetLastActorCoords" })
		CHECK(!plugin::allowed(n));  // still disabled: Unresolved / LocatedAbiUnproven dependencies

	// Actual evaluator re-entry revokes the old report and callback pointer.
	for (bool runtime : {true, false}) {
		set_state(true, true, true, as_catalog);
		FakeSettings* saved = dispatch(); CHECK(saved == &g_fake);
		state().runtime_is_240 = runtime;
		std::array<std::uint8_t, 1024> invalid{};
		std::uint32_t peoff = 64, size = invalid.size();
		std::memcpy(invalid.data() + 0x3c, &peoff, 4);
		std::memcpy(invalid.data() + peoff + 24 + 56, &size, 4);
		CHECK(!plugin::evaluate_loaded(reinterpret_cast<std::uintptr_t>(invalid.data()),
			"/nonexistent-guard-reset-fixture", [](const std::string&) {}));
		CHECK(state().evaluated); CHECK(state().runtime_is_240 == runtime);
		CHECK(!state().core_ok); CHECK(state().report.offsets.empty()); CHECK(state().report.natives.empty());
		g_calls = 0; saved = dispatch(); CHECK(saved == nullptr); CHECK(g_calls == 0);
		expect_refused("actual failed revalidation", true);
	}

	// 8. state reset after a validated dispatch: the next dispatch is refused and yields nullptr, so the
	//    value MessageHandler stores cannot keep a stale pointer usable
	set_state(true, true, false, as_catalog);
	expect_refused("reset to failed after validated", true);
	state() = plugin::GuardState{};
	expect_refused("reset to never evaluated", true);

	std::printf("test_plugin_gates: %d failures\n", failures);
	return failures ? 1 : 0;
}
