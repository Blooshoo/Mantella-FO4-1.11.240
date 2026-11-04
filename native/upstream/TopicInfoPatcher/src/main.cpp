// kalli/port-240: migrated from the pre-NG CommonLibF4 API onto libxse/commonlibf4 7c8c6f8 (the only
// supported build dependency for 1.11.240, see port240/include/mantella240/offsets240.h). Supported runtime:
// Fallout 4 1.11.240 only - the plugin refuses to load elsewhere (F4SEPlugin_Load) and declares nothing else.
// No <Windows.h>: libxse's REX::W32 declarations are used instead, so Windows macros (ERROR, MAX_PATH, ...)
// never meet REX names. Engine calls with no established 1.11.240 target are NOT compiled in; each omission
// carries a static_assert tripwire against offsets240.h (see offset_known / offset_verified there).
#include "Guard240.h"  // kalli/port-240: fail-closed offset guard (port240/plugin)
#include "mantella240/topic_patch.h"  // kalli/port-240: PatchTopicInfo decision logic (port240/include)
#include <array>
#include <atomic>
#include <filesystem>
#include <memory>
#include <mutex>

// Tripwire: `name` must be a known offset that is NOT Verified, because the code that would use it is
// omitted. Fails the build if offsets240.h upgrades it without the reviewed relocation being added here.
#define MANTELLA240_OMITTED_ENGINE_CALL(name) \
	static_assert(mantella240::offset_known(name) && !mantella240::offset_verified(name), \
		"offsets240.h verified " name " for 1.11.240: add its reviewed relocation here before enabling the native")

// kalli/port-240: every engine-touching native must pass the guard. Fails closed: the plugin only loads on
// 1.11.240, so a native running anywhere else (or before the guard ran) is refused.
static bool gate(std::string_view native) {
	if (mantella240::plugin::allowed(native)) return true;
	REX::WARN("{} disabled on 1.11.240 (see TopicInfoPatcher.log guard lines)", native);
	return false;
	}

// Logging: upstream init_log() (My Games/<game>/F4SE/TopicInfoPatcher.log, trace level, flush on trace,
// pattern "[%T.%e] [%=5t] [%L] %v") is reproduced by libxse F4SE::Init's InitLog with the InitInfo below.
// Upstream called F4SE::Init(a_f4se, false) = { .log = false } and then its own init_log().
static constexpr F4SE::InitInfo kInitInfo{
	.log = true,
	.logLevel = REX::ELogLevel::Trace,
	.logName = "TopicInfoPatcher",
	.logPattern = "[%T.%e] [%=5t] [%L] %v",
	.hook = false,  // this plugin installs no REL hooks
};

extern "C" DLLEXPORT bool F4SEAPI F4SEPlugin_Query(const F4SE::QueryInterface* a_f4se, F4SE::PluginInfo* a_info) {

	a_info->infoVersion = F4SE::PluginInfo::kVersion;
	a_info->name = Version::PROJECT.data();
	a_info->version = Version::MAJOR;

	
	if (a_f4se->IsEditor()) {
		REX::CRITICAL("loaded in editor");
		return false;
	}

	//const auto ver = a_f4se->RuntimeVersion();
	//if (ver < F4SE::RUNTIME_1_10_162 && ver != F4SE::RUNTIME_VR_1_2_72) {
	//	logger::critical("unsupported runtime v{}", ver.string());
	//	return false;
	//}

	return true;
}


// Functions to save and restore game settings
// Mantella temporarily modifies some settings to mute some NPC commentary
// During player conversation

RE::GameSettingCollection *GameSettings = nullptr;  // set only through acquire_game_settings (G01)

std::map<std::string, float> SettingsFloat;
std::map<std::string, int> SettingsInt;

bool saveFloat(std::monostate, std::string key) {
	bool ret = false;
	if (!gate("saveFloat")) return false;
	if (GameSettings) {
		RE::Setting* floatsetting = GameSettings->GetSetting(key);
		if (floatsetting != NULL && floatsetting->GetType() == RE::Setting::SETTING_TYPE::kFloat) {
			SettingsFloat.insert_or_assign(key, floatsetting->GetFloat());
			ret = true;
			}
		}
	return ret;
	}

bool saveInt(std::monostate, std::string key) {
	bool ret = false;
	if (!gate("saveInt")) return false;
	if (GameSettings) {
		RE::Setting* intsetting = GameSettings->GetSetting(key);
		if (intsetting != NULL && intsetting->GetType() == RE::Setting::SETTING_TYPE::kInt) {
			SettingsInt.insert_or_assign(key, intsetting->GetInt());
			ret = true;
			}
		}
	return ret;
	}

bool restoreFloat(std::monostate, std::string key) {
	bool ret = false;
	if (!gate("restoreFloat")) return false;
	if (GameSettings) {
		RE::Setting* floatsetting = GameSettings->GetSetting(key);
		if (SettingsFloat.contains(key) && floatsetting != NULL) {
			floatsetting->SetFloat(SettingsFloat.at(key));
			ret = true;
			}
		}
	return ret;
	}

bool restoreInt(std::monostate, std::string key) {
	bool ret = false;
	if (!gate("restoreInt")) return false;
	if (GameSettings) {
		RE::Setting* intsetting = GameSettings->GetSetting(key);
		if (SettingsInt.contains(key) && intsetting != NULL) {
			intsetting->SetInt(SettingsInt.at(key));
			ret = true;
			}
		}
	return ret;
	}

extern std::vector<float> GetLastActorCoords(std::monostate);  // Crosshair.cpp

void MessageHandler(F4SE::MessagingInterface::Message* a_msg) {
	if (!a_msg) {
		return;
	}

	switch (a_msg->type) {

	case F4SE::MessagingInterface::kGameDataReady: 	{
		REX::INFO("GameDataReady");
		// kalli/port-240 (re-review G01): this listener is registered even when the guard failed (degraded
		// load keeps the pure helper bound), so the singleton lookup - an engine read - is itself gated on
		// the validated offset. Refused: nothing touches the engine and GameSettings stays nullptr.
		GameSettings = mantella240::plugin::acquire_game_settings([] { return RE::GameSettingCollection::GetSingleton(); });
		if (!GameSettings) REX::WARN("GameDataReady: game settings unavailable (guard did not validate {})", mantella240::plugin::kGameSettingsSingleton);
		}
		break;
	case F4SE::MessagingInterface::kPostLoadGame:
	case F4SE::MessagingInterface::kNewGame:
		// kalli/port-240: upstream registered a crosshair event sink here (not on VR). Both its engine targets
		// have no 1.11.240 mapping, so no sink exists; GetLastActorCoords stays disabled (Crosshair.cpp).
		break;
	}
}

// Substitute for SUP_F4SE's function
bool isMenuModeActive(std::monostate) {
	if (!gate("isMenuModeActive")) return false;
	RE::UI* pUI = RE::UI::GetSingleton();
	return pUI->GetMenuOpen("DialogueMenu")
		|| pUI->GetMenuOpen("PipboyMenu")
		|| pUI->GetMenuOpen("VATSMenu")
		|| pUI->GetMenuOpen("LooksMenu")
		|| pUI->GetMenuOpen("BarterMenu")
		|| pUI->GetMenuOpen("WorkshopMenu")
		|| pUI->GetMenuOpen("Console")
		|| pUI->GetMenuOpen("SpecialMenu")
		|| pUI->GetMenuOpen("SimpleTextField");
	}

// kalli/port-240 (Sol review A02/A03): the placeholder text, the per-response patch bookkeeping and
// the decision logic live in port240/include/mantella240/topic_patch.h (tested on Linux). The old
// global lastText / in-place memset+strcpy_s into pooled string memory is gone.
static mantella240::TopicPatchState g_topicPatch;

// kalli/port-240 (Sol re-review N02): ownership protocol of topic_patch.h on the engine types. The new
// entry is acquired into a local, published with ONE atomic exchange of the response's pointer slot,
// and the response's reference on the old entry is transferred to g_topicPatch and never released.
// Replaces BGSLocalizedString::operator=, which releases the LIVE slot before GetEntry rewrites it.
// Written against libxse/commonlibf4 7c8c6f8 (RE/B/BSStringPool.h, BSFixedString.h,
// BGSLocalizedString.h, TESResponse.h), the declared migration target; NOT compiled.
namespace topic_slot {
	using Entry = RE::BSStringPool::Entry;
	// BGSLocalizedString == one BSFixedStringCS == one Entry* at offset 0 (libxse asserts sizeof 8).
	static_assert(sizeof(RE::BGSLocalizedString) == sizeof(Entry*), "response text must be one pool entry pointer");
	static_assert(sizeof(RE::BSFixedStringCS) == sizeof(Entry*), "BSFixedStringCS must be one pool entry pointer");
	static_assert(alignof(RE::TESResponse) >= std::atomic_ref<Entry*>::required_alignment);

	inline Entry*& slot(RE::TESResponse* r) { return *reinterpret_cast<Entry**>(std::addressof(r->responseText)); }

	struct Ops {
		std::string_view text(RE::TESResponse* r) const { return std::string_view(r->responseText); }
		Entry* acquire(std::string_view t) const {  // +1 reference, like the BSFixedStringCS constructor
			Entry* e = nullptr;
			if (!t.empty()) {
				const std::string z(t);  // NUL-terminated copy
				RE::GetEntry<char>(e, z.c_str(), true);  // case-sensitive pool, as BSFixedStringCS
			}
			return e;
		}
		Entry* exchange(RE::TESResponse* r, Entry* e) const noexcept {
			return std::atomic_ref<Entry*>(slot(r)).exchange(e, std::memory_order_acq_rel);
		}
		void release(Entry* e) const { RE::BSStringPool::Entry::release(e); }  // a local, never the slot
	};
}

// kalli/port-240: upstream CopyFuz (copy MantellaVoice00/00001ED2_1.fuz to "<name>_1.fuz") served only
// SetOverrideFileName below and is omitted with it.

// Replace the response text of a Mantella topic. kalli/port-240: publishes a NEW pooled string for this
// one response (topic_slot::Ops) instead of rewriting shared pool memory.
int PatchTopicInfo(std::monostate, RE::TESTopic *topic , std::string new_text) {
	using mantella240::PatchResult;
	if (!gate("PatchTopicInfo")) return static_cast<int>(PatchResult::Disabled);
	if (!topic) return static_cast<int>(PatchResult::NoTopic);
	// Sanity checks, to avoid touching the wrong record
	if (topic->numTopicInfos != 1 || !topic->topicInfos || !topic->topicInfos[0] || !topic->topicInfos[0]->responses.head) {
		REX::WARN("Topic {:X}: not a single-response Mantella topic", topic->formID);
		return static_cast<int>(PatchResult::BadRecord);
	}
	RE::TESTopicInfo* tpInfo = topic->topicInfos[0];
	RE::TESResponse* response = tpInfo->responses.head;
	REX::INFO("Topic {:X} TopicInfo {:X} Text: {}", topic->formID, tpInfo->formID, new_text);

	const PatchResult r = mantella240::patch_response(g_topicPatch, response, new_text, topic_slot::Ops{});
	if (r == PatchResult::TooLong) REX::WARN("Replacement too long! ({:d})", new_text.length());
	else if (r == PatchResult::RetentionFull) {
		if (g_topicPatch.sealed() || g_topicPatch.retained() + g_topicPatch.unrecorded() >= mantella240::kMaxRetainedEntries)
			REX::CRITICAL("PatchTopicInfo: string retention exhausted; refusing further patches this session (restart the game)");
		else
			REX::ERROR("PatchTopicInfo: could not reserve string bookkeeping (allocation failure); nothing was changed");
	}
	else if (r == PatchResult::NotMantella) REX::WARN("TopicInfo text is neither the Mantella placeholder nor our last text for this response");
	return static_cast<int>(r);
	}

// kalli/port-240: upstream called REL::ID(114073) on the BSAudioManager singleton ("doesn't seem to do
// anything?"). That ID is absent from the 1.11.240 Address Library, so the call is omitted; the native
// keeps its binding and the guard's documented disabled behaviour (no-op + log).
MANTELLA240_OMITTED_ENGINE_CALL("BSAudioManager::ClearCache");
void ClearCache(std::monostate) {
	if (!gate("ClearCache")) return;
	REX::CRITICAL("ClearCache: enabled without a 1.11.240 engine target; refusing");  // unreachable (tripwire)
	}

// Override the file name used for wav and lip files
// We can use any name instead of the Topic FormID 

// kalli/port-240: upstream used REL::VariantOffset(0x0621710, 0, 0x60C060) (pre-NG, NG = none, VR). There
// is no NG/AE and no 1.11.240 target, so the engine call (and its CopyFuz file copy) is omitted; the
// native keeps its binding and the guard's documented disabled behaviour (no-op + log). The current
// official Mantella FO4 scripts do not call it (offsets240.h).
MANTELLA240_OMITTED_ENGINE_CALL("TESTopicInfo::SetOverrideFileName");
void SetOverrideFileName(std::monostate, RE::TESTopic* this_p, std::string name) {
	if (!gate("SetOverrideFileName") || !this_p) return;
	REX::CRITICAL("SetOverrideFileName({}): enabled without a 1.11.240 engine target; refusing", name);  // unreachable (tripwire)
	}

std::string StringRemoveWhiteSpace(std::monostate, std::string str) {
	if (str.length() == 0) return str;

	auto start = str.begin();
	while (start != str.end() && std::isspace(*start)) {
		start++;
		}

	auto end = str.end();
	do {
		end--;
		} while (std::distance(start, end) > 0 && std::isspace(*end));

	return std::string(start, end + 1);
	}

// kalli/port-240: upstream REL::RelocationID(919230, 2229158) = (pre-NG ID, NG/AE ID). The 1.11.240 database
// resolves 2229158 to executable code (offsets240.h "TakeScreenshot", pattern-checked by the guard), but the
// (filename, type, sstype) ABI is unproven, so the guard never enables TakeScreenShot (LocatedAbiUnproven).
// Re-review G02: production tripwire, not only test_guard.cpp. Promoting the catalog entry to Verified is not
// an ABI proof; this build must fail until TakeScreenShot_internal gets a reviewed 1.11.240 ABI migration.
static_assert(mantella240::offset_known("TakeScreenshot") && !mantella240::offset_verified("TakeScreenshot"),
	"offsets240.h marks TakeScreenshot Verified: review and migrate the (filename, type, sstype) ABI of TakeScreenShot_internal for 1.11.240 first");
void TakeScreenShot_internal(char*filename, int filetype, int sstype) {
	static_assert(mantella240::offset_id("TakeScreenshot") == 2229158);
	using func_t = decltype(TakeScreenShot_internal);
	static REL::Relocation<func_t> func{ REL::ID(mantella240::offset_id("TakeScreenshot")) };

	func(filename, filetype, sstype);

	}

void TakeScreenShot(std::monostate, std::string filename, int filetype, int sstype) {
	if (!gate("TakeScreenShot")) return;
	TakeScreenShot_internal(filename.data(), filetype, sstype);
	}



bool RegisterFunctions(RE::BSScript::IVirtualMachine* a_VM) {
	a_VM->BindNativeMethod("TopicInfoPatcher", "PatchTopicInfo", PatchTopicInfo, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "ClearCache", ClearCache , true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "SetOverrideFileName", SetOverrideFileName, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "StringRemoveWhiteSpace", StringRemoveWhiteSpace, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "TakeScreenShot", TakeScreenShot, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "isMenuModeActive", isMenuModeActive, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "saveFloat", saveFloat, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "saveInt", saveInt, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "restoreFloat", restoreFloat, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "restoreInt", restoreInt, true);
	//a_VM->BindNativeMethod("TopicInfoPatcher", "GetLastCrossHairActor", GetLastCrossHairActor, true);
	a_VM->BindNativeMethod("TopicInfoPatcher", "GetLastActorCoords", GetLastActorCoords, true);
	//a_VM->BindNativeMethod("TopicInfoPatcher", "GetLastCrossHairRef", GetLastCrosshairRef, true);
	//a_VM->BindNativeMethod("TopicInfoPatcher", "GetActorName", GetActorName, true);
	return true;
	}




extern "C" DLLEXPORT bool F4SEAPI F4SEPlugin_Load(const F4SE::LoadInterface* a_f4se) {
	F4SE::Init(a_f4se, kInitInfo);

	// kalli/port-240: supported runtime is exactly 1.11.240 (no editor, no VR - libxse 7c8c6f8 has no VR
	// runtime). Refuse before registering anything; the declared CompatibleVersions say the same.
	if (a_f4se->IsEditor()) {
		REX::CRITICAL("loaded in editor, refusing");
		return false;
		}
	if (a_f4se->RuntimeVersion() != REL::Version(1, 11, 240, 0)) {
		REX::CRITICAL("runtime is not 1.11.240, refusing to load");
		return false;
		}

	// kalli/port-240: evaluate the guard against the loaded image before any native can run.
	{
		auto& st = mantella240::plugin::state();
		st.runtime_is_240 = true;
		std::array<wchar_t, 32768> exe_path{};  // wide API: install paths may be non-ASCII
		const auto n = REX::W32::GetModuleFileNameW(nullptr, exe_path.data(), static_cast<std::uint32_t>(exe_path.size()));
		bool ok = false;
		if (n == 0 || n >= exe_path.size()) {
			REX::CRITICAL("guard: cannot determine the executable path");
		} else {
			std::filesystem::path addrlib = std::filesystem::path(std::wstring_view(exe_path.data(), n)).remove_filename() / "Data" / "F4SE" / "Plugins" / "version-1-11-240-0.bin";
			ok = mantella240::plugin::evaluate_loaded(REX::FModule::GetExecutingModule().GetBaseAddress(), addrlib,
				[](const std::string& m) { REX::INFO("{}", m); });
		}
		if (!ok) {
			REX::CRITICAL("1.11.240 guard failed: engine-touching natives stay disabled");
			}
	}


	F4SE::GetMessagingInterface()->RegisterListener(MessageHandler);

	const auto papyrus = F4SE::GetPapyrusInterface();
	if (!papyrus || !papyrus->Register(RegisterFunctions)) {
		REX::CRITICAL("Failed to register Papyrus functions!");
	} else {
		REX::INFO("Registered Papyrus functions.");
	}
	REX::INFO("{} v{}", Version::PROJECT, Version::NAME);

	return true;
	}

F4SE_EXPORT constinit auto F4SEPlugin_Version = []() noexcept {
	F4SE::PluginVersionData data{};

	data.PluginName(Version::PROJECT);
	data.PluginVersion(REL::Version(0,9,4,0));
	data.AuthorName("peteben");
	data.UsesAddressLibrary(true);
	data.UsesSigScanning(false);
	// kalli/port-240: must NOT claim 1.11.137-layout independence. Together with UsesAddressLibrary(true)
	// official F4SE 0.7.x treats the plugin as version independent and loads it on every 1.11.x runtime,
	// ignoring the list below (PluginManager::CheckPluginCompatibility). Only 1.11.240 is guarded/verified.
	data.IsLayoutDependent(false);
	data.HasNoStructUse(false);
	// kalli/port-240: ONLY 1.11.240. The libxse build has no pre-NG/NG/VR relocations (upstream's VariantOffset /
	// RelocationID paths are gone) and the guard verifies only 1.11.240, so listing other runtimes would be a
	// false claim. Official upstream builds remain the choice for other runtimes.
	data.CompatibleVersions({ REL::Version(1, 11, 240, 0) });

	return data;
	}();
