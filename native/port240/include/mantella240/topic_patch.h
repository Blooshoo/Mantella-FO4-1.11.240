// Engine-independent decision logic for TopicInfoPatcher's PatchTopicInfo (1.11.240 port).
//
// Replaces upstream's in-place rewrite of the response text (Sol review A02/A03):
//   * upstream validated `len <= 150` and then called strcpy_s(dst, 150, src), which needs room for
//     the terminator: exactly 150 bytes invoked MSVC's invalid-parameter handler (process abort);
//   * it used ONE global `lastText` as proof of buffer identity/capacity, so any other single-
//     response topic whose text equalled the last subtitle received a 151-byte memset into a
//     buffer that may be a few bytes long;
//   * the text lives in a BGSLocalizedString backed by a POOLED BSFixedStringCS entry: casting away
//     const and rewriting it also rewrote every other owner of that pool entry.
// The port never writes string memory. It assigns a NEW pooled string to this one response through
// CommonLib's BGSLocalizedString::operator=, and tracks per response (not per text) what it assigned,
// so only (a) the untouched Mantella placeholder or (b) a response this plugin itself patched before
// and that still carries exactly what was assigned, can be patched.
#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>

namespace mantella240 {

// Placeholder text of every Mantella topic info in Mantella.esp (150 bytes).
inline constexpr std::string_view kTopicPlaceholder =
	"Mantella01Mantella02Mantella03Mantella04Mantella05Mantella06Mantella07Mantella08Mantella09Mantella10"
	"Mantella11Mantella12Mantella13Mantella14Mantella15";
static_assert(kTopicPlaceholder.size() == 150);

inline constexpr std::size_t kMaxSubtitleBytes = 150;  // upstream contract (-2 above it)

// Return codes of the Papyrus native (unchanged meaning; -5 is new).
enum class PatchResult : int {
	Ok = 0,
	NoTopic = -1,      // None topic
	TooLong = -2,      // > 150 bytes
	NotMantella = -3,  // response is neither the placeholder nor what this plugin assigned to it
	Disabled = -4,     // guard disabled the native
	BadRecord = -5,    // topic/topic info/response pointers missing or more than one topic info
	RetentionFull = -6,  // N02 retention cap reached: patching stops for this game session (fails closed)
};

// String lifetime vs. engine readers (Sol re-review N02; supersedes the 4096-entry FIFO).
//
// The response text is ONE pooled-string reference (libxse 7c8c6f8: BGSLocalizedString = one
// BSFixedStringCS = one BSStringPool::Entry*, refcount in Entry::_flags, 14 bits, saturating).
// Engine readers do not take our lock: they load that pointer and then acquire their own reference
// (SubtitleInfo / HUDSubtitleDisplayData hold owning BSFixedStringCS) or use the raw c_str for a
// while (TESResponse::GetResponseText). Neither is a quiescence protocol we can observe. libxse's
// BGSLocalizedString::operator= is also unsafe against them: it calls the engine's
// Entry::release(slot) on the LIVE response slot before GetEntry(slot, ...) writes it again, so a
// reader can catch the slot mid-update and the old entry can hit refcount 0 between its load and
// its acquire.
//
// Ownership protocol instead (patch_response below), all under the state lock:
//   1. the new entry is acquired into a LOCAL (the response slot is not touched);
//   2. it is published with ONE atomic pointer exchange on the response slot - a reader observes
//      the old or the new pointer, never null, never a partly written value;
//   3. the reference the response held on the old entry is TRANSFERRED to this state and is never
//      released (one reference per distinct entry; a duplicate reference is dropped at once, which
//      cannot reach zero because one is still owned).
// Hence no entry this plugin ever displaced can be freed because of this plugin, however long an
// engine reader keeps a raw pointer, with no reader quiescence needed. Cost: at most
// kMaxRetainedEntries distinct entries (each <= 150 bytes of text + pool header) for the process
// lifetime; at the cap patching fails closed (RetentionFull) instead of releasing anything.
//
// NOT proven here (engine side, runtime acceptance): that the engine itself never writes the
// response slot concurrently (e.g. a form reload), that readers load the slot with a single aligned
// 8-byte load (x64 codegen; formally a data race in C++ terms), and the BGSLocalizedString layout
// on the real 1.11.240 binary (libxse static_asserts sizeof == 8, member at offset 0). Localized
// "<ID=" strings never reach the swap: they can never equal the placeholder or our own text.
inline constexpr std::size_t kMaxRetainedEntries = 65536;

class TopicPatchState {
public:
	// `current` = the response's current text; `key` identifies the response object.
	[[nodiscard]] PatchResult check_locked(const void* key, std::string_view current, std::string_view new_text) const {
		if (new_text.size() > kMaxSubtitleBytes) return PatchResult::TooLong;
		if (current == kTopicPlaceholder) return PatchResult::Ok;
		auto it = assigned_.find(key);
		if (it != assigned_.end() && it->second == current) return PatchResult::Ok;
		return PatchResult::NotMantella;
	}
	void record_locked(const void* key, std::string_view text) {
		if (assigned_.size() >= kMaxTracked && !assigned_.contains(key)) assigned_.clear();  // bounded; fails closed (-3)
		assigned_[key] = std::string(text);
	}
	// Retention of displaced entries (protocol step 3). Identity = entry address.
	[[nodiscard]] bool retention_room_locked() const { return retained_.size() < retain_cap_; }
	// Takes over ONE reference on `entry`. Returns true when a reference to it was already owned:
	// the caller must then release the new one (the owned one keeps the count above zero).
	// On allocation failure the reference is kept unrecorded (a leak is safe, a release is not).
	[[nodiscard]] bool adopt_locked(const void* entry) noexcept {
		if (!entry) return false;
		try { return !retained_.insert(entry).second; } catch (...) { return false; }
	}
	[[nodiscard]] bool retains(const void* entry) const { std::lock_guard lk(mu_); return retained_.contains(entry); }
	[[nodiscard]] std::size_t retained() const { std::lock_guard lk(mu_); return retained_.size(); }
	explicit TopicPatchState(std::size_t retain_cap = kMaxRetainedEntries) : retain_cap_(retain_cap ? retain_cap : 1) {}
	[[nodiscard]] std::unique_lock<std::mutex> lock() const { return std::unique_lock(mu_); }
	[[nodiscard]] std::size_t tracked() const { std::lock_guard lk(mu_); return assigned_.size(); }

private:
	static constexpr std::size_t kMaxTracked = 4096;  // Mantella.esp has a few hundred topic infos
	mutable std::mutex mu_;
	std::unordered_map<const void*, std::string> assigned_;
	std::unordered_set<const void*> retained_;  // entries we own one reference on, never released
	std::size_t retain_cap_;
};

// Shared by the plugin (engine types) and the Linux tests (fake pool). `ops` must provide, all
// called with the state lock held:
//   text(resp)             -> std::string_view of the response's current text
//   acquire(text)          -> Entry* carrying ONE new reference for `text` (nullptr for ""); may
//                             throw (nothing has been changed yet)
//   exchange(resp, entry)  -> publishes `entry` in the response slot with ONE atomic pointer
//                             exchange and returns the previous Entry* (and the reference the
//                             response held on it); noexcept
//   release(entry)         -> drops one reference
template <class Resp, class Ops>
PatchResult patch_response(TopicPatchState& st, Resp* resp, std::string_view new_text, Ops&& ops) {
	if (!resp) return PatchResult::BadRecord;
	auto lk = st.lock();
	const PatchResult r = st.check_locked(resp, ops.text(resp), new_text);
	if (r != PatchResult::Ok) return r;
	if (!st.retention_room_locked()) return PatchResult::RetentionFull;  // before anything changes
	auto* fresh = ops.acquire(new_text);
	auto* old = ops.exchange(resp, fresh);
	if (st.adopt_locked(old)) ops.release(old);
	st.record_locked(resp, new_text);
	return PatchResult::Ok;
}

}  // namespace mantella240
