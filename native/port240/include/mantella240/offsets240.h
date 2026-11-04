// Single source of truth for every engine offset the TopicInfoPatcher port depends on,
// for Fallout 4 1.11.240 (Fallout4.exe SHA-256 fdcef37ac1230af6d0b0050eb2142b139ef3a867b37b9211fb6edfcc646072f8,
// Address Library version-1-11-240-0.bin SHA-256 65985cc2259384a13cffb538d74776e422e62b0cd3766485a000620242b72b06).
//
// Evidence: original local report (not bundled): Mantella-native-240-feasibility-2026-11-03.md
// and research-mantella-20261103/evidence/{offline-symbol-matrix.json,240-form-lookup-disassembly.txt}.
#pragma once

#include <array>
#include <cstdint>
#include <string_view>

namespace mantella240 {

enum class Kind : std::uint8_t { Function, DataSlot };

enum class Status : std::uint8_t {
	Verified,            // ID resolves, location semantics corroborated (CommonLib + F4SE + disassembly)
	LocatedAbiUnproven,  // ID resolves to code, but calling convention/behaviour not proven -> never enabled
	Unresolved,          // no trustworthy .240 target -> never enabled
};

struct RipRef {
	std::uint32_t insn_offset;  // instruction start, relative to the function
	std::uint32_t disp_offset;  // offset of the rel32 within the instruction
	std::uint32_t insn_len;     // instruction length (rip = start + len)
	std::uint64_t target_id;    // Address Library ID the reference must resolve to
};

struct OffsetSpec {
	std::string_view name;
	std::uint64_t id;  // 0 = no ID exists
	Kind kind;
	Status status;
	std::string_view pattern;  // functions only; ?? = wildcard (rel32 operands)
	std::array<RipRef, 2> refs;
	std::uint8_t nrefs;
	std::string_view evidence;
};

inline constexpr std::uint64_t kID_TESForm_GetFormByNumericID = 2193092;
inline constexpr std::uint64_t kID_TESForm_AllForms = 4796465;
inline constexpr std::uint64_t kID_TESForm_AllFormsMapLock = 4796476;
inline constexpr std::uint64_t kID_PlayerCharacter_Singleton = 4798212;
inline constexpr std::uint64_t kID_UI_Singleton = 4796314;
inline constexpr std::uint64_t kID_GameSettingCollection_Singleton = 4797590;
inline constexpr std::uint64_t kID_BSAudioManager_Singleton = 2703058;
inline constexpr std::uint64_t kID_TakeScreenshot = 2229158;
inline constexpr std::uint64_t kID_Legacy_AudioClearCache = 114073;
// libxse/commonlibf4 7c8c6f8 RE/IDs.h ID::BSStringPool::GetEntry_char / Entry::Release. PatchTopicInfo's
// BGSLocalizedString assignment reaches exactly these through libxse, so the plugin MUST be built on
// libxse (the pre-NG CommonLibF4 a22ea1b uses other IDs and is not supported for 1.11.240).
inline constexpr std::uint64_t kID_BSStringPool_GetEntry_char = 2268729;
inline constexpr std::uint64_t kID_BSStringPool_Entry_Release = 2268720;  // upstream pre-NG ID; absent from .240 database

inline constexpr std::array kOffsets240 = {
	OffsetSpec{ "TESForm::GetFormByNumericID", kID_TESForm_GetFormByNumericID, Kind::Function, Status::Verified,
		"48 89 5C 24 08 57 48 83 EC 20 8B F9 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 48 8B 1D ?? ?? ?? ?? 48 85 DB",
		{ RipRef{ 0x0C, 3, 7, kID_TESForm_AllFormsMapLock }, RipRef{ 0x18, 3, 7, kID_TESForm_AllForms } }, 2,
		"RVA 0x311B80; CommonLib + official F4SE GameForms.cpp; disassembly locks map lock then reads map" },
	OffsetSpec{ "TESForm::AllForms", kID_TESForm_AllForms, Kind::DataSlot, Status::Verified, {}, {}, 0,
		"RVA 0x30EBEC8; referenced by lookup" },
	OffsetSpec{ "TESForm::AllFormsMapLock", kID_TESForm_AllFormsMapLock, Kind::DataSlot, Status::Verified, {}, {}, 0,
		"RVA 0x30EBF18; referenced by lookup" },
	OffsetSpec{ "BSStringPool::GetEntry<char>", kID_BSStringPool_GetEntry_char, Kind::Function, Status::Verified,
		"44 88 44 24 18 48 89 4C 24 08 55 53 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 E1 48 81 EC 88 00 00 00", {}, 0,
		"RVA 0x167DD20; F4SE 0.7.9 StringCache::Ref::ctor (0x167C1E0) and ::Set (0x167C320) call it with (ref, str, bool) "
		"(call rel32 at +0x18 / +0x19, disassembled 2026-11-03); prologue spills r8b = 3-arg ABI as in libxse" },
	OffsetSpec{ "BSStringPool::Entry::Release", kID_BSStringPool_Entry_Release, Kind::Function, Status::Verified,
		"40 56 48 83 EC 40 48 83 39 00 48 8B F1 0F 84 ?? ?? ?? ?? 4C 89 6C 24 28 E8 ?? ?? ?? ??", {}, 0,
		"RVA 0x167D4A0 == F4SE 0.7.9 StringCache::Ref::Release (GameTypes.h), also called by Ref::Set +0x23" },
	OffsetSpec{ "PlayerCharacter::Singleton", kID_PlayerCharacter_Singleton, Kind::DataSlot, Status::Verified, {}, {}, 0,
		"RVA 0x31EDE50; CommonLib PlayerCharacter::Singleton (.191 hardcoded 0x31E2D50 is wrong)" },
	OffsetSpec{ "UI::Singleton", kID_UI_Singleton, Kind::DataSlot, Status::Verified, {}, {}, 0,
		"RVA 0x30E8930; CommonLib UI::Singleton + official F4SE GameMenus.cpp" },
	OffsetSpec{ "GameSettingCollection::Singleton", kID_GameSettingCollection_Singleton, Kind::DataSlot, Status::Verified, {}, {}, 0,
		"RVA 0x30F8920; CommonLib + official F4SE GameSettings.cpp" },
	OffsetSpec{ "BSAudioManager::Singleton", kID_BSAudioManager_Singleton, Kind::DataSlot, Status::Verified, {}, {}, 0,
		"CommonLib current IDs.h BSAudioManager::Singleton; location only, no cache-clear function known" },
	OffsetSpec{ "TakeScreenshot", kID_TakeScreenshot, Kind::Function, Status::LocatedAbiUnproven,
		"48 89 5C 24 08 48 89 74 24 10 57 48 81 EC 50 02 00 00 80 3D", {}, 0,
		"RVA 0xC43F20 executable; (filename,type,sstype) ABI not proven -> disabled" },
	OffsetSpec{ "BSAudioManager::ClearCache", kID_Legacy_AudioClearCache, Kind::Function, Status::Unresolved, {}, {}, 0,
		"upstream REL::ID(114073) absent from .240 database; needs engine RE" },
	OffsetSpec{ "TESTopicInfo::SetOverrideFileName", 0, Kind::Function, Status::Unresolved, {}, {}, 0,
		"upstream VariantOffset(0x621710,0,0x60C060): no NG/AE target; core voice-file selection; needs engine RE" },
	OffsetSpec{ "ViewCasterUpdate event source", 0, Kind::DataSlot, Status::Unresolved, {}, {}, 0,
		"upstream VariantOffset(0x5A5F758,0x2FF12F0,0); AE layout unknown" },
	OffsetSpec{ "BSPointerHandle lookup (crosshair)", 0, Kind::Function, Status::Unresolved, {}, {}, 0,
		"upstream VariantOffset(0xAB60,0x24C240,0); use CommonLib handle API after review" },
};

// Compile-time lookups for plugin tripwires. TopicInfoPatcher is compiled WITHOUT any engine call whose
// 1.11.240 target is not established (no guessed relocations); next to each such omission the plugin
// static_asserts offset_known(name) && !offset_verified(name). Upgrading an offset to Verified here then
// fails the plugin build until the reviewed relocation is added there. offset_known stops a misspelt
// name from making the tripwire vacuous.
[[nodiscard]] constexpr bool offset_known(std::string_view name) {
	for (const auto& o : kOffsets240) if (o.name == name) return true;
	return false;
}
[[nodiscard]] constexpr bool offset_verified(std::string_view name) {
	for (const auto& o : kOffsets240) if (o.name == name) return o.status == Status::Verified;
	return false;
}
[[nodiscard]] constexpr std::uint64_t offset_id(std::string_view name) {
	for (const auto& o : kOffsets240) if (o.name == name) return o.id;
	return 0;
}

// Natives exported to Papyrus (TopicInfoPatcher.psc API is unchanged from upstream, so the
// official Mantella TopicInfoPatcher.pex keeps matching). Each native lists the offsets it needs.
struct NativeSpec {
	std::string_view name;
	std::array<std::string_view, 3> needs;
	std::uint8_t nneeds;
	std::string_view when_disabled;  // documented Papyrus-visible behaviour
};

inline constexpr std::array kNatives240 = {
	NativeSpec{ "PatchTopicInfo", { "BSStringPool::GetEntry<char>", "BSStringPool::Entry::Release" }, 2, "returns -4 (subtitle not patched). TESTopic/TESTopicInfo/TESResponse LAYOUT is source-corroborated only (CommonLib headers); patch is gated on placeholder text / per-response bookkeeping (topic_patch.h)" },
	NativeSpec{ "StringRemoveWhiteSpace", {}, 0, "n/a (pure)" },
	NativeSpec{ "isMenuModeActive", { "UI::Singleton" }, 1, "returns false" },
	NativeSpec{ "saveFloat", { "GameSettingCollection::Singleton" }, 1, "returns false" },
	NativeSpec{ "saveInt", { "GameSettingCollection::Singleton" }, 1, "returns false" },
	NativeSpec{ "restoreFloat", { "GameSettingCollection::Singleton" }, 1, "returns false" },
	NativeSpec{ "restoreInt", { "GameSettingCollection::Singleton" }, 1, "returns false" },
	NativeSpec{ "ClearCache", { "BSAudioManager::ClearCache" }, 1, "no-op + log; not called by current official scripts" },
	NativeSpec{ "SetOverrideFileName", { "TESTopicInfo::SetOverrideFileName" }, 1, "no-op + log; NOT called by current official Mantella FO4 scripts (speech uses pre-named topic-info voice files)" },
	NativeSpec{ "TakeScreenShot", { "TakeScreenshot" }, 1, "no-op + log (Mantella Vision screenshots unavailable)" },
	NativeSpec{ "GetLastActorCoords", { "ViewCasterUpdate event source", "BSPointerHandle lookup (crosshair)" }, 2, "returns empty array -> crosshair-hotkey start fails (Papyrus array error, no crash); use activation perk or disable crosshair tracking" },
};

}  // namespace mantella240
