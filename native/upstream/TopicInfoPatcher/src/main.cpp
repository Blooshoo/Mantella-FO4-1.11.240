#include <Windows.h>
#include "Guard240.h"  // kalli/port-240: fail-closed offset guard (port240/plugin)
#include "mantella240/topic_patch.h"  // kalli/port-240: PatchTopicInfo decision logic (port240/include)
#include <atomic>
#include <memory>
#include <mutex>

bool isVR;

// kalli/port-240: on runtime 1.11.240 every engine-touching native must pass the guard.
// Other runtimes keep upstream behaviour unchanged.
static bool gate(std::string_view native) {
	auto& st = mantella240::plugin::state();
	if (!st.runtime_is_240) return true;
	if (mantella240::plugin::allowed(native)) return true;
	logger::warn("{} disabled on 1.11.240 (see TopicInfoPatcher.log guard lines)", native);
	return false;
	}

void init_log() {
	std::optional<std::filesystem::path> logpath = logger::log_directory();

	const char* plugin_name = Version::PROJECT.data();
	*logpath /= fmt::format(FMT_STRING("{}.log"), plugin_name);
	auto sink = std::make_shared<spdlog::sinks::basic_file_sink_mt>(logpath->string(), true);

	auto log = std::make_shared<spdlog::logger>("global log"s, std::move(sink));

	log->set_level(spdlog::level::trace);
	log->flush_on(spdlog::level::trace);

	spdlog::set_default_logger(std::move(log));
	spdlog::set_pattern("[%T.%e] [%=5t] [%L] %v"s);

	logger::info("{} v{}", Version::PROJECT, Version::NAME);
	}

extern "C" DLLEXPORT bool F4SEAPI F4SEPlugin_Query(const F4SE::QueryInterface* a_f4se, F4SE::PluginInfo* a_info) {

	a_info->infoVersion = F4SE::PluginInfo::kVersion;
	a_info->name = Version::PROJECT.data();
	a_info->version = Version::MAJOR;

	
	if (a_f4se->IsEditor()) {
		logger::critical("loaded in editor");
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

RE::GameSettingCollection *GameSettings;

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

extern void RegisterCrosshair();
extern std::vector<RE::Actor*> GetLastCrossHairActor(std::monostate);
extern std::vector<float> GetLastActorCoords(std::monostate);

void MessageHandler(F4SE::MessagingInterface::Message* a_msg) {
	if (!a_msg) {
		return;
	}

	switch (a_msg->type) {

	case F4SE::MessagingInterface::kGameDataReady: 	{
		logger::info("GameDataReady");
		GameSettings = RE::GameSettingCollection::GetSingleton();
		}
		break;
	case F4SE::MessagingInterface::kPostLoadGame:
	case F4SE::MessagingInterface::kNewGame:
		if (!REL::Module::IsVR() && gate("GetLastActorCoords")) {
			RegisterCrosshair();
			}
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

void CopyFuz(std::string fname) {							// Copy the generated FUZ file to the requested name
	char exe_path[_MAX_PATH];

	GetModuleFileNameA(NULL, exe_path, _MAX_PATH);			// Fallout exe
	std::filesystem::path app_path(exe_path);
	std::filesystem::path base_path(app_path.remove_filename());
	base_path += "Data\\Sound\\Voice\\Mantella.esp\\MantellaVoice00";		// Path to Mantella voices directory
	std::filesystem::path src_path(base_path / "00001ED2_1.fuz");
	std::filesystem::path target_file(base_path / std::format("{}_1.fuz", fname));
	CopyFile(src_path.c_str(), target_file.c_str(), false);
	}

// Replace the response text of a Mantella topic. kalli/port-240: publishes a NEW pooled string for this
// one response (topic_slot::Ops) instead of rewriting shared pool memory.
int PatchTopicInfo(std::monostate, RE::TESTopic *topic , std::string new_text) {
	using mantella240::PatchResult;
	if (!gate("PatchTopicInfo")) return static_cast<int>(PatchResult::Disabled);
	if (!topic) return static_cast<int>(PatchResult::NoTopic);
	// Sanity checks, to avoid touching the wrong record
	if (topic->numTopicInfos != 1 || !topic->topicInfos || !topic->topicInfos[0] || !topic->topicInfos[0]->responses.head) {
		logger::warn("Topic {:X}: not a single-response Mantella topic", topic->formID);
		return static_cast<int>(PatchResult::BadRecord);
	}
	RE::TESTopicInfo* tpInfo = topic->topicInfos[0];
	RE::TESResponse* response = tpInfo->responses.head;
	logger::info("Topic {:X} TopicInfo {:X} Text: {}", topic->formID, tpInfo->formID, new_text);

	const PatchResult r = mantella240::patch_response(g_topicPatch, response, new_text, topic_slot::Ops{});
	if (r == PatchResult::TooLong) logger::warn("Replacement too long! ({:d})", new_text.length());
	else if (r == PatchResult::RetentionFull) {
		if (g_topicPatch.sealed() || g_topicPatch.retained() + g_topicPatch.unrecorded() >= mantella240::kMaxRetainedEntries)
			logger::critical("PatchTopicInfo: string retention exhausted; refusing further patches this session (restart the game)");
		else
			logger::error("PatchTopicInfo: could not reserve string bookkeeping (allocation failure); nothing was changed");
	}
	else if (r == PatchResult::NotMantella) logger::warn("TopicInfo text is neither the Mantella placeholder nor our last text for this response");
	return static_cast<int>(r);
	}

void ClearCacheInternal(RE::BSAudioManager *) {									// Doesn't seem to do anything?
	RE::BSAudioManager* AudioMan = RE::BSAudioManager::GetSingleton();

	using func_t = decltype(ClearCacheInternal);
	static REL::Relocation<func_t> func{ REL::ID(114073) };
	func(AudioMan);
	}

void ClearCache(std::monostate) {
	if (!gate("ClearCache")) return;
	ClearCacheInternal(nullptr);
	}

// Override the file name used for wav and lip files
// We can use any name instead of the Topic FormID 

void SetOverrideFileName_internal(RE::TESTopicInfo *Info, char * name, uint64_t , uint64_t){
	using func_t = decltype(SetOverrideFileName_internal);
	static REL::Relocation<func_t> func{ REL::VariantOffset(0x0621710, 0, 0x60C060)};	// Pre-NG, NG, VR

	logger::info("Info: {:p} Name; {:p}", (void*)Info, (void*)name);
	func(Info, name, 0, 0);
	}

void SetOverrideFileName(std::monostate, RE::TESTopic* this_p, std::string name) {
	if (!gate("SetOverrideFileName") || !this_p) return;
	RE::TESTopicInfo* tpInfo = this_p->topicInfos[0];

	if (name != "00001ED2") {
		CopyFuz(name);
		}

	logger::info("SetOverrideFileName Info: {:p} name: {}", (void*) this_p, name.data());
	SetOverrideFileName_internal(tpInfo, name.data(), 0, 0);
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

void TakeScreenShot_internal(char*filename, int filetype, int sstype) {
	using func_t = decltype(TakeScreenShot_internal);
	static REL::Relocation<func_t> func{ REL::RelocationID(919230, 2229158) };

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
	F4SE::Init(a_f4se,false);

	init_log();

	if (REL::Module::IsVR()) {
		logger::info("is VR");
		REL::Version F4SEver = a_f4se->F4SEVersion();
		logger::info("F4SE Version is: {:d}.{:d}.{:d}.{:d}", F4SEver.major(), F4SEver.minor(), F4SEver.build(), F4SEver.patch());
		if (F4SEver < REL::Version(0, 6, 21, 0)) {
			MessageBox(NULL, L"Please update to F4SEVR Version 0.6.21.", L"Newer version of F4SEVR required!", 0);
			ExitProcess(02);
			}
		}
	else {
		logger::info("Not VR");
		}

	// kalli/port-240: evaluate the guard against the loaded image before any native can run.
	if (a_f4se->RuntimeVersion() == REL::Version(1, 11, 240, 0)) {
		auto& st = mantella240::plugin::state();
		st.runtime_is_240 = true;
		char exe_path[_MAX_PATH];
		GetModuleFileNameA(NULL, exe_path, _MAX_PATH);
		std::filesystem::path addrlib = std::filesystem::path(exe_path).remove_filename() / "Data" / "F4SE" / "Plugins" / "version-1-11-240-0.bin";
		bool ok = mantella240::plugin::evaluate_loaded(REL::Module::get().base(), addrlib,
			[](const std::string& m) { logger::info("{}", m); });
		if (!ok) {
			logger::critical("1.11.240 guard failed: engine-touching natives stay disabled");
			}
		}


	F4SE::GetMessagingInterface()->RegisterListener(MessageHandler);

	const auto papyrus = F4SE::GetPapyrusInterface();
	if (!papyrus || !papyrus->Register(RegisterFunctions)) {
		logger::critical("Failed to register Papyrus functions!");
	} else {
		logger::info("Registered Papyrus functions.");
	}
	logger::info("{} v{}", Version::PROJECT, Version::NAME);

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
	data.CompatibleVersions({ F4SE::RUNTIME_1_10_984, F4SE::RUNTIME_1_10_163, F4SE::RUNTIME_LATEST_VR, REL::Version(1, 11, 240, 0) });  // 1.11.240: guarded, see port240/

	return data;
	}();
