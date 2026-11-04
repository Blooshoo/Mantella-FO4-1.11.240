// Thread-safe typed dictionary store behind the F4SE_HTTP Papyrus API (1.11.240 port).
//
// Original work for this port (MIT, like F4SE_HTTP). Replaces upstream cdff839
// SKSE_HTTP_TypedDictionary.cpp / TypedDictionary.cpp, keeping the Papyrus-visible behaviour the
// official Mantella FO4 scripts rely on, and fixing these upstream defects (each has a test):
//   D1 type confusion: upstream getX() static_pointer_cast'ed whatever was stored (getFloat on an
//      int JSON value read the int's bits; getString on an int read garbage as std::string -> crash).
//      Here every getter checks the stored type and applies the documented coercions below.
//   D2 data race: upstream mutated one unordered_map from the HTTP callback thread and Papyrus
//      threads with no lock. Here one mutex guards all state.
//   D3 handle space: official scripts encode errors as `handle + 100000` (GetHandle), but upstream
//      handles start at 0 and grow without bound, so after 100000 dictionaries replies would be
//      misread as errors, and 0 aliased the getNestedDictionary default. Here handles live in
//      [1, 99999].
//   D3b handle lifetime (Sol review A05): a live dictionary is NEVER evicted and a handle number is
//      never reissued while its dictionary is live. The store is bounded by a dictionary count and a
//      retained-byte budget and FAILS CLOSED when full (create() -> 0, setters -> false, oversized
//      replies -> error reply). Memory is reclaimed only at script lifecycle boundaries the official
//      scripts define: clearAllDictionaries() (MantellaListenerScript calls it on a timer while no
//      conversation runs) and the start of a new conversation (HttpCore keeps only the graph
//      reachable from the start request). A reclaimed number is reissued only after the round-robin
//      counter has cycled the whole 1..99999 range, i.e. never within one conversation epoch.
//      Residual risk (documented, not solvable inside the legacy int-handle protocol): a script
//      that keeps a handle across those boundaries can, after a full counter cycle, read an
//      unrelated dictionary. The official scripts do not keep handles across them.
//   D4 unbounded recursion: nested-dictionary cycles (setNestedDictionary(h,"k",h)) or deep JSON
//      overflowed the stack in upstream's recursive conversion. Depth/cycle limited here.
//   D6 unbounded expansion (Sol review A04): repeated references in an acyclic graph expand
//      independently, so a 33-node chain of two-element arrays expands to 2^32 JSON nodes while the
//      store mutex is held. to_json now enforces aggregate node and byte budgets and fails the
//      request instead.
//   D5 hasKey did not lowercase its key while every setter did; here hasKey lowercases too.
//
// Coercions (documented Papyrus-visible contract; upstream behaviour for these cases was undefined):
//   getInt:   int -> value; float -> truncated toward zero (Papyrus `as Int`), saturated; bool -> 0/1
//   getFloat: float -> value; int -> (float); bool -> 0.0/1.0
//   getBool:  bool -> value; int/float -> != 0
//   getIntArray / getFloatArray: element-wise int<->float with the same rules
//   any other type mismatch, missing key, unknown/evicted handle -> the caller's default / empty array
#pragma once

#include "json.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <variant>
#include <vector>

namespace f4sehttp240 {

inline std::string lower(std::string s) {
	std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
	return s;
}

struct NestedRef { int handle = 0; };
struct NestedArray { std::vector<int> handles; };

using DictValue = std::variant<std::string, std::int32_t, float, bool, NestedRef,
	std::vector<std::string>, std::vector<std::int32_t>, std::vector<float>, std::vector<bool>, NestedArray>;

// Upstream getTypeName() strings, kept for diagnostics and tests.
inline const char* type_name(const DictValue& v) {
	static constexpr const char* names[] = { "string", "int", "float", "bool", "NestedDictionary",
		"stringArray", "intArray", "floatArray", "boolArray", "NestedDictionaryArray" };
	return names[v.index()];
}

inline std::int32_t float_to_int(float f) {
	if (!std::isfinite(f)) return 0;
	double t = std::trunc(static_cast<double>(f));
	if (t >= 2147483647.0) return INT32_MAX;
	if (t <= -2147483648.0) return INT32_MIN;
	return static_cast<std::int32_t>(t);
}

inline std::int32_t int64_to_int(std::int64_t v) {
	return v > INT32_MAX ? INT32_MAX : v < INT32_MIN ? INT32_MIN : static_cast<std::int32_t>(v);
}

// Aggregate budget for one dictionary -> JSON conversion (request body).
struct ExpansionBudget {
	std::size_t max_nodes = 200000;
	std::size_t max_bytes = 8u << 20;
};

class DictStore {
public:
	static constexpr int kFirstHandle = 1;
	static constexpr int kLastHandle = 99999;      // GetHandle encodes errors as handle + 100000
	static constexpr std::size_t kDefaultCapacity = 50000;
	static constexpr std::size_t kErrorReserve = 64;        // slots only error replies may use
	static constexpr std::size_t kDefaultMaxBytes = 256u << 20;
	static constexpr std::size_t kMaxConvertDepth = 32;

	using Budget = ExpansionBudget;

	explicit DictStore(std::size_t capacity = kDefaultCapacity, std::size_t max_bytes = kDefaultMaxBytes) :
		capacity_(std::clamp<std::size_t>(capacity, 1, static_cast<std::size_t>(kLastHandle - kFirstHandle) - kErrorReserve)),
		max_bytes_(max_bytes) {}

	// 0 when the store is full (fail closed; never evicts a live dictionary). `for_error` may use the
	// small reserve so an error reply can still be delivered when ordinary slots are exhausted.
	int create(bool for_error = false) {
		std::lock_guard lk(mu_);
		return create_locked(for_error);
	}

	void clear_all() {
		std::lock_guard lk(mu_);
		dicts_.clear();
		bytes_ = 0;
	}

	// Lifecycle reclaim: drop every dictionary not reachable from `root` (iterative, visits each
	// dictionary at most once). Returns the number dropped.
	std::size_t retain_only_reachable(int root) {
		std::lock_guard lk(mu_);
		std::unordered_set<int> keep;
		std::vector<int> stack{ root };
		while (!stack.empty()) {
			int h = stack.back();
			stack.pop_back();
			auto it = dicts_.find(h);
			if (it == dicts_.end() || !keep.insert(h).second) continue;
			for (const auto& [k, v] : it->second.items) {
				(void)k;
				if (auto* n = std::get_if<NestedRef>(&v)) stack.push_back(n->handle);
				else if (auto* a = std::get_if<NestedArray>(&v)) stack.insert(stack.end(), a->handles.begin(), a->handles.end());
			}
		}
		std::size_t dropped = 0;
		for (auto it = dicts_.begin(); it != dicts_.end();) {
			if (keep.contains(it->first)) { ++it; continue; }
			bytes_ -= it->second.bytes;
			it = dicts_.erase(it);
			++dropped;
		}
		return dropped;
	}

	[[nodiscard]] std::size_t size() const { std::lock_guard lk(mu_); return dicts_.size(); }
	[[nodiscard]] bool exists(int h) const { std::lock_guard lk(mu_); return dicts_.contains(h); }
	[[nodiscard]] std::size_t retained_bytes() const { std::lock_guard lk(mu_); return bytes_; }
	[[nodiscard]] std::size_t rejected() const { std::lock_guard lk(mu_); return rejected_; }

	// ---- getters -------------------------------------------------------------------------------
	std::string get_string(int h, std::string key, std::string def) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (v) if (auto* s = std::get_if<std::string>(v)) return *s;
		return def;
	}
	std::int32_t get_int(int h, std::string key, std::int32_t def) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (!v) return def;
		if (auto* i = std::get_if<std::int32_t>(v)) return *i;
		if (auto* f = std::get_if<float>(v)) return float_to_int(*f);
		if (auto* b = std::get_if<bool>(v)) return *b ? 1 : 0;
		return def;
	}
	float get_float(int h, std::string key, float def) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (!v) return def;
		if (auto* f = std::get_if<float>(v)) return *f;
		if (auto* i = std::get_if<std::int32_t>(v)) return static_cast<float>(*i);
		if (auto* b = std::get_if<bool>(v)) return *b ? 1.0f : 0.0f;
		return def;
	}
	bool get_bool(int h, std::string key, bool def) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (!v) return def;
		if (auto* b = std::get_if<bool>(v)) return *b;
		if (auto* i = std::get_if<std::int32_t>(v)) return *i != 0;
		if (auto* f = std::get_if<float>(v)) return *f != 0.0f;
		return def;
	}
	int get_nested(int h, std::string key, int def) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (v) if (auto* n = std::get_if<NestedRef>(v)) return n->handle;
		return def;
	}
	std::vector<std::string> get_string_array(int h, std::string key) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (v) if (auto* a = std::get_if<std::vector<std::string>>(v)) return *a;
		return {};
	}
	std::vector<std::int32_t> get_int_array(int h, std::string key) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (!v) return {};
		if (auto* a = std::get_if<std::vector<std::int32_t>>(v)) return *a;
		if (auto* a = std::get_if<std::vector<float>>(v)) {
			std::vector<std::int32_t> out;
			out.reserve(a->size());
			for (float f : *a) out.push_back(float_to_int(f));
			return out;
		}
		return {};
	}
	std::vector<float> get_float_array(int h, std::string key) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (!v) return {};
		if (auto* a = std::get_if<std::vector<float>>(v)) return *a;
		if (auto* a = std::get_if<std::vector<std::int32_t>>(v)) return { a->begin(), a->end() };
		return {};
	}
	std::vector<bool> get_bool_array(int h, std::string key) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (v) if (auto* a = std::get_if<std::vector<bool>>(v)) return *a;
		return {};
	}
	std::vector<std::int32_t> get_nested_array(int h, std::string key) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (v) if (auto* a = std::get_if<NestedArray>(v)) return a->handles;
		return {};
	}
	bool has_key(int h, std::string key) const {
		std::lock_guard lk(mu_);
		return find(h, key) != nullptr;
	}
	std::optional<std::string> value_type_name(int h, std::string key) const {
		std::lock_guard lk(mu_);
		auto* v = find(h, key);
		if (!v) return std::nullopt;
		return type_name(*v);
	}

	// ---- setters (return false when the handle is unknown, the value is rejected, or the store's
	//      retained-byte budget would be exceeded) -------------------------------------------------
	bool set(int h, std::string key, DictValue value) {
		std::lock_guard lk(mu_);
		return set_locked(h, lower(std::move(key)), std::move(value));
	}
	// Upstream setString: rejects (does not store) values that are not valid UTF-8.
	bool set_string(int h, std::string key, std::string value) {
		if (!json::valid_utf8(value)) return false;
		return set(h, std::move(key), std::move(value));
	}
	// Upstream setStringArray: stores the valid elements, returns false if any were dropped.
	bool set_string_array(int h, std::string key, const std::vector<std::string>& value) {
		std::vector<std::string> kept;
		bool all = true;
		for (const auto& s : value) { if (json::valid_utf8(s)) kept.push_back(s); else all = false; }
		bool stored = set(h, std::move(key), std::move(kept));
		return stored && all;
	}

	// ---- JSON bridge ---------------------------------------------------------------------------
	// Request body: dictionary -> JSON object. Nested handles are followed up to kMaxConvertDepth
	// and cycles are cut (the repeated reference becomes null) instead of recursing forever.
	// Returns nullopt when the expansion exceeds `budget` (the caller turns that into an error
	// reply); the work done under the lock is therefore bounded by the budget.
	std::optional<json::Value> to_json(int h, Budget budget = {}) const {
		std::lock_guard lk(mu_);
		std::unordered_set<int> path;
		Spend spend{ budget };
		json::Value v = to_json_locked(h, path, 0, spend);
		if (spend.exceeded) return std::nullopt;
		return v;
	}

	// Reply body: JSON -> new dictionary handle; keys lowercased recursively (upstream rule).
	// Array classification mirrors upstream: all-strings, all-integers, all-floats, all-bools,
	// all-objects; additionally a mixed int/float array becomes a float array (upstream dropped it).
	// Anything else (null, nested arrays, mixed types) is dropped, as upstream did.
	// Returns 0 (nothing stored) if `value` is not an object, if importing it would create more than
	// half the store's capacity in dictionaries, or if it does not fit in the free dictionary slots /
	// retained-byte budget. All-or-nothing: a partial import is rolled back.
	int from_json(const json::Value& value) {
		if (value.type != json::Type::Object) return 0;
		const std::size_t need = count_dicts(value, 0);
		if (need > capacity_ / 2) return 0;
		std::lock_guard lk(mu_);
		if (dicts_.size() + need > capacity_) { ++rejected_; return 0; }
		std::vector<int> created;
		bool ok = true;
		int h = from_json_locked(value, 0, created, ok);
		if (!ok || !h) {
			for (int c : created) { auto it = dicts_.find(c); if (it != dicts_.end()) { bytes_ -= it->second.bytes; dicts_.erase(it); } }
			++rejected_;
			return 0;
		}
		return h;
	}
	[[nodiscard]] std::size_t capacity() const { return capacity_; }

private:
	struct Dict {
		std::unordered_map<std::string, DictValue> items;
		std::size_t bytes = 0;  // approximate retained size, for the store budget
	};
	struct Spend {
		Budget budget;
		std::size_t nodes = 0, bytes = 0;
		bool exceeded = false;
		bool take(std::size_t n, std::size_t b) {
			if (exceeded) return false;
			nodes += n; bytes += b;
			if (nodes > budget.max_nodes || bytes > budget.max_bytes) exceeded = true;
			return !exceeded;
		}
	};

	static constexpr std::size_t kSlotOverhead = 64;
	static std::size_t value_bytes(const std::string& key, const DictValue& v) {
		std::size_t n = kSlotOverhead + key.size();
		std::visit([&](const auto& x) {
			using T = std::decay_t<decltype(x)>;
			if constexpr (std::is_same_v<T, std::string>) n += x.size();
			else if constexpr (std::is_same_v<T, std::vector<std::string>>) { for (const auto& e : x) n += 32 + e.size(); }
			else if constexpr (std::is_same_v<T, std::vector<std::int32_t>> || std::is_same_v<T, std::vector<float>>) n += x.size() * 4;
			else if constexpr (std::is_same_v<T, std::vector<bool>>) n += x.size() / 8 + 1;
			else if constexpr (std::is_same_v<T, NestedArray>) n += x.handles.size() * 4;
		}, v);
		return n;
	}

	const DictValue* find(int h, const std::string& key) const {
		auto it = dicts_.find(h);
		if (it == dicts_.end()) return nullptr;
		auto kit = it->second.items.find(lower(key));
		return kit == it->second.items.end() ? nullptr : &kit->second;
	}

	int create_locked(bool for_error) {
		const std::size_t limit = capacity_ + (for_error ? kErrorReserve : 0);
		if (dicts_.size() >= limit || bytes_ + kSlotOverhead > max_bytes_ + (for_error ? kErrorReserve * 512 : 0)) {
			++rejected_;
			return 0;  // fail closed: never evict a dictionary a script may still hold
		}
		for (int tries = 0; tries <= kLastHandle; ++tries) {
			int h = next_;
			next_ = next_ >= kLastHandle ? kFirstHandle : next_ + 1;
			if (!dicts_.contains(h)) {
				auto& d = dicts_.emplace(h, Dict{}).first->second;
				d.bytes = kSlotOverhead;
				bytes_ += d.bytes;
				return h;
			}
		}
		return 0;  // unreachable: capacity_ + reserve < handle range
	}

	bool set_locked(int h, std::string key, DictValue value, bool for_error = false) {
		auto it = dicts_.find(h);
		if (it == dicts_.end()) return false;
		Dict& d = it->second;
		const std::size_t add = value_bytes(key, value);
		auto old = d.items.find(key);
		const std::size_t sub = old == d.items.end() ? 0 : value_bytes(key, old->second);
		if (add > sub && bytes_ + (add - sub) > max_bytes_ + (for_error ? kErrorReserve * 512 : 0)) { ++rejected_; return false; }
		bytes_ = bytes_ + add - sub;
		d.bytes = d.bytes + add - sub;
		d.items[std::move(key)] = std::move(value);
		return true;
	}

public:
	// Error replies: small, use the reserve, never fail on the byte budget because of their size.
	bool set_error(int h, std::string key, std::string msg) {
		if (msg.size() > 400) msg.resize(400);
		std::lock_guard lk(mu_);
		return set_locked(h, lower(std::move(key)), std::move(msg), /*for_error=*/true);
	}

private:
	json::Value to_json_locked(int h, std::unordered_set<int>& path, std::size_t depth, Spend& spend) const {
		if (!spend.take(1, 0)) return json::Value::null();
		auto it = dicts_.find(h);
		if (it == dicts_.end() || depth > kMaxConvertDepth || path.contains(h)) return json::Value::null();
		path.insert(h);
		json::Value out = json::Value::object();
		for (const auto& [k, v] : it->second.items) {
			if (!spend.take(1, k.size() + 4)) break;
			json::Value jv;
			std::visit([&](const auto& x) {
				using T = std::decay_t<decltype(x)>;
				if constexpr (std::is_same_v<T, std::string>) { if (!spend.take(0, x.size() + 2)) return; jv = json::Value::string(x); }
				else if constexpr (std::is_same_v<T, std::int32_t>) jv = json::Value::integer(x);
				else if constexpr (std::is_same_v<T, float>) jv = json::Value::number(static_cast<double>(x));
				else if constexpr (std::is_same_v<T, bool>) jv = json::Value::boolean(x);
				else if constexpr (std::is_same_v<T, NestedRef>) jv = to_json_locked(x.handle, path, depth + 1, spend);
				else if constexpr (std::is_same_v<T, NestedArray>) {
					jv = json::Value::array();
					for (int sub : x.handles) {
						if (spend.exceeded) break;
						jv.arr.push_back(to_json_locked(sub, path, depth + 1, spend));
					}
				} else {
					jv = json::Value::array();
					if (!spend.take(x.size(), x.size() * 8)) return;
					for (const auto& e : x) {
						using E = std::decay_t<decltype(e)>;
						if constexpr (std::is_same_v<E, std::string>) { if (!spend.take(0, e.size() + 2)) return; jv.arr.push_back(json::Value::string(e)); }
						else if constexpr (std::is_same_v<E, std::int32_t>) jv.arr.push_back(json::Value::integer(e));
						else if constexpr (std::is_same_v<E, float>) jv.arr.push_back(json::Value::number(static_cast<double>(e)));
						else jv.arr.push_back(json::Value::boolean(static_cast<bool>(e)));
					}
				}
			}, v);
			out.obj.emplace_back(k, std::move(jv));
		}
		path.erase(h);
		return out;
	}

	// Number of dictionaries from_json_locked would create for `o` (mirrors its depth rules).
	static std::size_t count_dicts(const json::Value& o, std::size_t depth) {
		std::size_t n = 1;
		for (const auto& [k, v] : o.obj) {
			(void)k;
			if (v.type == json::Type::Object) {
				if (depth < kMaxConvertDepth) n += count_dicts(v, depth + 1);
			} else if (v.type == json::Type::Array && depth < kMaxConvertDepth && !v.arr.empty() &&
			           std::all_of(v.arr.begin(), v.arr.end(), [](const json::Value& e) { return e.type == json::Type::Object; })) {
				for (const auto& e : v.arr) n += count_dicts(e, depth + 1);
			}
		}
		return n;
	}

	int from_json_locked(const json::Value& o, std::size_t depth, std::vector<int>& created, bool& ok) {
		if (!ok) return 0;
		int h = create_locked(false);
		if (!h) { ok = false; return 0; }
		created.push_back(h);
		auto put = [&](const std::string& key, DictValue v) { if (ok && !set_locked(h, key, std::move(v))) ok = false; };
		for (const auto& [rawKey, v] : o.obj) {
			if (!ok) break;
			std::string key = lower(rawKey);
			switch (v.type) {
				case json::Type::String: put(key, v.s); break;
				case json::Type::Int: put(key, int64_to_int(v.i)); break;
				case json::Type::Float: put(key, static_cast<float>(v.f)); break;
				case json::Type::Bool: put(key, v.b); break;
				case json::Type::Object:
					if (depth < kMaxConvertDepth) { int sub = from_json_locked(v, depth + 1, created, ok); put(key, NestedRef{ sub }); }
					break;
				case json::Type::Array: set_array(h, key, v, depth, created, ok); break;
				case json::Type::Null: break;
			}
		}
		return ok ? h : 0;
	}

	void set_array(int h, const std::string& key, const json::Value& a, std::size_t depth, std::vector<int>& created, bool& ok) {
		auto set_locked = [&](int hh, const std::string& k, DictValue v) { if (ok && !this->set_locked(hh, k, std::move(v))) ok = false; };
		auto all = [&](auto pred) { return std::all_of(a.arr.begin(), a.arr.end(), pred); };
		using J = json::Value;
		if (all([](const J& e) { return e.type == json::Type::String; })) {  // also the empty array (upstream order)
			std::vector<std::string> out;
			for (const auto& e : a.arr) out.push_back(e.s);
			set_locked(h, key, std::move(out));
		} else if (all([](const J& e) { return e.type == json::Type::Int; })) {
			std::vector<std::int32_t> out;
			for (const auto& e : a.arr) out.push_back(int64_to_int(e.i));
			set_locked(h, key, std::move(out));
		} else if (all([](const J& e) { return e.type == json::Type::Float || e.type == json::Type::Int; })) {
			std::vector<float> out;
			for (const auto& e : a.arr) out.push_back(e.type == json::Type::Int ? static_cast<float>(e.i) : static_cast<float>(e.f));
			set_locked(h, key, std::move(out));
		} else if (all([](const J& e) { return e.type == json::Type::Bool; })) {
			std::vector<bool> out;
			for (const auto& e : a.arr) out.push_back(e.b);
			set_locked(h, key, std::move(out));
		} else if (all([](const J& e) { return e.type == json::Type::Object; })) {
			if (depth >= kMaxConvertDepth) return;
			NestedArray out;
			for (const auto& e : a.arr) { if (!ok) return; out.handles.push_back(from_json_locked(e, depth + 1, created, ok)); }
			set_locked(h, key, std::move(out));
		}
	}

	mutable std::mutex mu_;
	std::unordered_map<int, Dict> dicts_;
	std::size_t capacity_;
	std::size_t max_bytes_;
	std::size_t bytes_ = 0;
	std::size_t rejected_ = 0;
	int next_ = kFirstHandle;
};

}  // namespace f4sehttp240
