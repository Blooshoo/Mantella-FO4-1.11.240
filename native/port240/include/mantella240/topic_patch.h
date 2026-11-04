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
};

// Thread-safety (Sol re-review N02): check, assignment and bookkeeping of one patch run as ONE
// critical section (patch_response holds the state mutex across all three), so concurrent VM calls
// cannot interleave. Engine readers (subtitle UI) do not take this lock: they observe the
// response's 8-byte-aligned string pointer either before or after the single pointer store, and the
// plugin keeps every replaced entry referenced for a long retention window (kRetain), so a reader
// still holding the old c_str keeps a live string. That window is a practical bound, not a proven
// reader-quiescence protocol; runtime acceptance must confirm it (residual risk, documented).
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
	[[nodiscard]] std::unique_lock<std::mutex> lock() const { return std::unique_lock(mu_); }
	[[nodiscard]] std::size_t tracked() const { std::lock_guard lk(mu_); return assigned_.size(); }

private:
	static constexpr std::size_t kMaxTracked = 4096;  // Mantella.esp has a few hundred topic infos
	mutable std::mutex mu_;
	std::unordered_map<const void*, std::string> assigned_;
};

// Replaced string entries kept referenced (see thread-safety note). One spoken line takes seconds,
// so 4096 retained entries are hours of dialogue; each costs one pooled string reference.
inline constexpr std::size_t kRetainReplacedStrings = 4096;

// Shared by the plugin (engine types) and the Linux tests (fakes).
//   get_text(resp) -> std::string_view of the current text
//   assign(resp, std::string_view)  -> replaces the response's string with a NEW pooled entry
//                                      (called with the state lock held)
template <class Resp, class GetText, class Assign>
PatchResult patch_response(TopicPatchState& st, Resp* resp, std::string_view new_text, GetText&& get_text, Assign&& assign) {
	if (!resp) return PatchResult::BadRecord;
	auto lk = st.lock();
	const PatchResult r = st.check_locked(resp, get_text(resp), new_text);
	if (r != PatchResult::Ok) return r;
	assign(resp, new_text);
	st.record_locked(resp, new_text);
	return PatchResult::Ok;
}

}  // namespace mantella240
