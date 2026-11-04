// Papyrus <-> native contract for F4SE_HTTP on 1.11.240: one row per native the OFFICIAL Mantella FO4
// F4SE_HTTP.psc declares (active declarations only; `valueType` and `RegisterForHttpEvent` are
// commented out there and are therefore not bound). tests/test_http_core.cpp parses the official
// PSC and fails if this table and the script disagree in name, return type or parameter types.
//
// Marshalling / ownership contract (implemented by plugin/F4SE_HTTP240.cpp on libxse CommonLibF4):
//   * Every native is a global (std::monostate self) native bound with BindNativeMethod.
//   * String parameters arrive as BSFixedString (VM-owned, pooled, ref-counted); the native copies
//     them into std::string before returning. No BSFixedString is retained, mutated, or released by
//     hand; no page-protection / vectored-exception tricks are used.
//   * Array parameters: the VM-owned BSScript::Array is copied element-wise into a std::vector; the
//     native never keeps the Array pointer. A None array argument is treated as empty (libxse
//     7c8c6f8 dereferences a null Array here -> fixed by patches/libxse-7c8c6f8-null-array.patch).
//   * String/array returns: the native returns std::string / std::vector by value; libxse creates
//     a fresh BSFixedString / VM Array (vm->CreateArray) owned by the VM's ref counting. The plugin
//     holds no reference after return.
//   * Natives never block on the network: sendLocalhostHttpRequest only enqueues; replies are made
//     visible exclusively through GetHandle().
#pragma once

#include <array>
#include <string_view>

namespace f4sehttp240 {

struct NativeContract {
	std::string_view name;
	std::string_view ret;     // Papyrus return type, "" = none
	std::string_view params;  // Papyrus parameter types, comma separated, no spaces
	std::string_view on_invalid;  // behaviour for unknown handle / missing key / type mismatch
};

inline constexpr std::array kContract = {
	NativeContract{ "sendLocalhostHttpRequest", "", "int,int,string,int", "queues an error reply (GetHandle -> h+100000) instead of silently doing nothing" },
	NativeContract{ "createDictionary", "int", "", "never fails; oldest dictionary evicted at capacity" },
	NativeContract{ "clearAllDictionaries", "", "", "n/a" },
	NativeContract{ "getString", "string", "int,string,string", "returns default" },
	NativeContract{ "getInt", "int", "int,string,int", "returns default; float truncated, bool 0/1" },
	NativeContract{ "getFloat", "float", "int,string,float", "returns default; int/bool converted" },
	NativeContract{ "getBool", "bool", "int,string,bool", "returns default; int/float != 0" },
	NativeContract{ "getNestedDictionary", "int", "int,string,int", "returns default" },
	NativeContract{ "getIntArray", "int[]", "int,string", "returns empty array; float elements truncated" },
	NativeContract{ "getFloatArray", "float[]", "int,string", "returns empty array; int elements converted" },
	NativeContract{ "getStringArray", "string[]", "int,string", "returns empty array" },
	NativeContract{ "getBoolArray", "bool[]", "int,string", "returns empty array" },
	NativeContract{ "getNestedDictionariesArray", "int[]", "int,string", "returns empty array" },
	NativeContract{ "setString", "bool", "int,string,string", "false if handle unknown or value not UTF-8" },
	NativeContract{ "setInt", "", "int,string,int", "ignored if handle unknown" },
	NativeContract{ "setFloat", "", "int,string,float", "ignored if handle unknown" },
	NativeContract{ "setBool", "", "int,string,bool", "ignored if handle unknown" },
	NativeContract{ "setNestedDictionary", "", "int,string,int", "ignored if handle unknown" },
	NativeContract{ "setIntArray", "", "int,string,int[]", "ignored if handle unknown; None array = empty" },
	NativeContract{ "setFloatArray", "", "int,string,float[]", "ignored if handle unknown; None array = empty" },
	NativeContract{ "setStringArray", "bool", "int,string,string[]", "false if handle unknown or any element dropped (not UTF-8)" },
	NativeContract{ "setBoolArray", "", "int,string,bool[]", "ignored if handle unknown; None array = empty" },
	NativeContract{ "setNestedDictionariesArray", "", "int,string,int[]", "ignored if handle unknown; None array = empty" },
	NativeContract{ "GetHandle", "int", "", "-1 when nothing is pending" },
	NativeContract{ "hasKey", "bool", "int,string", "false" },
};

}  // namespace f4sehttp240
