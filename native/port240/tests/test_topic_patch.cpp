// PatchTopicInfo decision logic (Sol review A02/A03) and string-lifetime protocol (re-review N02)
// against a fake pooled-string model.
//
// The fake pool mirrors libxse 7c8c6f8's BSStringPool as far as lifetime goes: interned immutable
// entries with an intrusive reference count, looked up by text, and FREED (delete) when the count
// reaches zero. A response holds exactly one reference in one pointer-sized slot. Built under
// ASan/UBSan and TSan by run_tests.sh, so a reader touching a freed entry is a hard failure.
//
// Readers model the two engine patterns: load the slot then acquire an own reference (SubtitleInfo /
// HUDSubtitleDisplayData keep owning BSFixedStringCS), and keep a raw c_str without any reference
// (TESResponse::GetResponseText callers). Engine-side facts NOT modelled are listed in topic_patch.h.
#include "mantella240/topic_patch.h"

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)

using namespace mantella240;

// ------------------------------------------------------------------ deterministic allocation faults
// Every operator new in this process is counted; when armed, exactly the n-th allocation from now
// throws std::bad_alloc. Armed only on the main thread while no other test thread runs.
static std::atomic<long> g_allocs{ 0 };
static std::atomic<long> g_fail_countdown{ 0 };  // 0 = disarmed
void* operator new(std::size_t n) {
	g_allocs.fetch_add(1, std::memory_order_relaxed);
	if (g_fail_countdown.load(std::memory_order_relaxed) > 0 && g_fail_countdown.fetch_sub(1) == 1) throw std::bad_alloc();
	if (void* p = std::malloc(n ? n : 1)) return p;
	throw std::bad_alloc();
}
// The replacement operator new above is malloc-based, so free() is the matching deallocation;
// GCC's -Wmismatched-new-delete cannot see that for replaced global operators.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wmismatched-new-delete"
void operator delete(void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
#pragma GCC diagnostic pop

namespace mantella240 {
struct TopicPatchStateTestAccess {
	// Simulates "the displaced entry cannot be recorded" after publication (re-review M01).
	static void drop_spare(TopicPatchState& s) { s.spare_ = {}; }
};
}  // namespace mantella240

struct Entry {
	std::atomic<int> refs{ 0 };
	std::string text;
};

class Pool {
public:
	~Pool() { for (auto& [k, e] : live_) delete e; }
	// GetEntry: one new reference (nullptr for "", like an empty BSFixedString). Strong exception
	// guarantee, so allocation-fault runs leave the pool consistent.
	Entry* acquire(std::string_view s) {
		if (s.empty()) return nullptr;
		std::string key(s);
		std::lock_guard lk(mu_);
		auto it = live_.find(key);
		if (it == live_.end()) {
			auto fresh = std::make_unique<Entry>();
			fresh->text = key;
			it = live_.emplace(std::move(key), fresh.get()).first;
			fresh.release();
		}
		it->second->refs.fetch_add(1, std::memory_order_relaxed);
		return it->second;
	}
	// Entry::acquire on an entry the caller only has a pointer to (UAF if it was freed).
	static void addref(Entry* e) { e->refs.fetch_add(1, std::memory_order_relaxed); }
	void release(Entry* e) {
		if (!e) return;
		std::lock_guard lk(mu_);
		if (e->refs.fetch_sub(1, std::memory_order_acq_rel) == 1) {
			live_.erase(e->text);
			delete e;
			freed_.fetch_add(1, std::memory_order_relaxed);
		}
	}
	[[nodiscard]] int refs(std::string_view s) {
		std::lock_guard lk(mu_);
		auto it = live_.find(std::string(s));
		return it == live_.end() ? 0 : it->second->refs.load();
	}
	[[nodiscard]] bool alive(std::string_view s) { std::lock_guard lk(mu_); return live_.contains(std::string(s)); }
	[[nodiscard]] long freed() const { return freed_.load(); }
	[[nodiscard]] long total_refs() { std::lock_guard lk(mu_); long n = 0; for (auto& [k, e] : live_) n += e->refs.load(); return n; }

private:
	std::mutex mu_;
	std::unordered_map<std::string, Entry*> live_;  // non-owning index, like the engine pool
	std::atomic<long> freed_{ 0 };
};

struct FakeResponse {
	std::atomic<Entry*> slot{ nullptr };  // one owned reference (models the aligned 8-byte member)
	FakeResponse(Pool& p, std::string_view t) { slot.store(p.acquire(t)); }
	[[nodiscard]] std::string_view text() const { auto* e = slot.load(std::memory_order_acquire); return e ? std::string_view(e->text) : std::string_view{}; }
};

struct Ops {
	Pool& pool;
	std::string_view text(FakeResponse* r) const { return r->text(); }
	Entry* acquire(std::string_view t) const { return pool.acquire(t); }
	Entry* exchange(FakeResponse* r, Entry* e) const noexcept { return r->slot.exchange(e, std::memory_order_acq_rel); }
	void release(Entry* e) const { pool.release(e); }
};

static PatchResult patch(TopicPatchState& st, Pool& pool, FakeResponse* r, std::string_view t) {
	return patch_response(st, r, t, Ops{ pool });
}

// Ops that make recording of the displaced entry impossible after publication.
struct UnrecordableOps : Ops {
	TopicPatchState& st;
	Entry* exchange(FakeResponse* r, Entry* e) const noexcept {
		TopicPatchStateTestAccess::drop_spare(st);  // state lock is held by patch_response
		return Ops::exchange(r, e);
	}
};

// Ownership accounting for one response: every reference alive is the response's or the state's.
static bool balanced(Pool& pool, const TopicPatchState& st, const FakeResponse& r) {
	const long slot = r.slot.load() ? 1 : 0;
	return pool.total_refs() == slot + static_cast<long>(st.retained() + st.unrecorded());
}

static void test_decision_logic() {
	Pool pool;
	TopicPatchState st;

	// A02: 149 / 150 / 151 bytes. Upstream accepted 150 and then strcpy_s(dst,150,...) aborted.
	for (std::size_t n : { 149u, 150u }) {
		FakeResponse r(pool, kTopicPlaceholder);
		CHECK(patch(st, pool, &r, std::string(n, 'A')) == PatchResult::Ok);
		CHECK(r.text() == std::string(n, 'A'));
	}
	{
		FakeResponse r(pool, kTopicPlaceholder);
		CHECK(patch(st, pool, &r, std::string(151, 'A')) == PatchResult::TooLong);
		CHECK(r.text() == kTopicPlaceholder);
	}

	// A03: two Mantella topics share ONE pooled placeholder entry. Patching one must not change the other.
	FakeResponse t1(pool, kTopicPlaceholder), t2(pool, kTopicPlaceholder);
	CHECK(t1.slot.load() == t2.slot.load());
	CHECK(patch(st, pool, &t1, "Hi") == PatchResult::Ok);
	CHECK(t1.text() == "Hi");
	CHECK(t2.text() == kTopicPlaceholder);

	// A03: an unrelated single-response topic whose text happens to equal the last subtitle ("Hi")
	// passed upstream's global lastText check and got a 151-byte memset. Must be rejected.
	FakeResponse other(pool, "Hi");
	CHECK(patch(st, pool, &other, "Overwrite") == PatchResult::NotMantella);
	CHECK(other.text() == "Hi");

	// The patched Mantella response can be patched again (per-response bookkeeping) ...
	CHECK(patch(st, pool, &t1, "Second line") == PatchResult::Ok);
	CHECK(t1.text() == "Second line");
	// ... but not if something else changed its text since.
	pool.release(t1.slot.exchange(pool.acquire("changed by someone else")));
	CHECK(patch(st, pool, &t1, "Third") == PatchResult::NotMantella);
	// a reloaded form shows the placeholder again and is patchable
	pool.release(t1.slot.exchange(pool.acquire(kTopicPlaceholder)));
	CHECK(patch(st, pool, &t1, "After reload") == PatchResult::Ok);

	CHECK(patch(st, pool, static_cast<FakeResponse*>(nullptr), "x") == PatchResult::BadRecord);
	for (FakeResponse* r : { &t1, &t2, &other }) pool.release(r->slot.exchange(nullptr));
}

// N02: a reader keeps a RAW pointer (no reference) to a patched line while 20,000 further lines are
// patched in - five times the old 4096-entry FIFO, after which that FIFO released the entry and
// the read was a use-after-free. Nothing this plugin displaced may ever be freed.
static void test_raw_reader_outlives_any_window() {
	Pool pool;
	TopicPatchState st;
	FakeResponse r(pool, kTopicPlaceholder);
	CHECK(patch(st, pool, &r, "line 0") == PatchResult::Ok);
	const char* raw = r.slot.load()->text.c_str();  // no reference taken
	for (int i = 1; i <= 20000; ++i) CHECK(patch(st, pool, &r, "line " + std::to_string(i)) == PatchResult::Ok);
	CHECK(std::string_view(raw) == "line 0");        // ASan: heap-use-after-free if it was released
	CHECK(pool.freed() == 0);
	CHECK(pool.refs("line 0") == 1);                 // exactly the reference transferred to the state
	CHECK(st.retained() == 20001);                   // placeholder + lines 0..19999 (20000 is installed)
	CHECK(pool.refs("line 20000") == 1);             // the response's own reference
	pool.release(r.slot.exchange(nullptr));
}

// N02: engine-style readers race with patches on another thread: load the slot then acquire their
// own reference (the window where the old protocol could drop the last reference), and raw readers.
static void test_concurrent_engine_readers() {
	Pool pool;
	TopicPatchState st;
	FakeResponse r(pool, kTopicPlaceholder);
	std::atomic<bool> done{ false };
	std::atomic<long> bad{ 0 }, reads{ 0 };
	auto valid = [](std::string_view t) { return t == kTopicPlaceholder || t.starts_with("line "); };
	std::vector<std::thread> readers;
	for (int k = 0; k < 2; ++k)
		readers.emplace_back([&] {  // SubtitleInfo-like: load, acquire, use, release
			while (!done.load(std::memory_order_acquire)) {
				Entry* e = r.slot.load(std::memory_order_acquire);
				if (!e) { bad.fetch_add(1); continue; }
				Pool::addref(e);
				if (!valid(e->text)) bad.fetch_add(1);
				pool.release(e);
				reads.fetch_add(1, std::memory_order_relaxed);
			}
		});
	for (int k = 0; k < 2; ++k)
		readers.emplace_back([&] {  // GetResponseText-like: raw c_str, no reference, read later
			std::vector<const char*> held;
			while (!done.load(std::memory_order_acquire)) {
				Entry* e = r.slot.load(std::memory_order_acquire);
				if (!e) { bad.fetch_add(1); continue; }
				held.push_back(e->text.c_str());
				if (held.size() == 64) {
					for (const char* p : held) if (!valid(p)) bad.fetch_add(1);
					held.clear();
				}
			}
			for (const char* p : held) if (!valid(p)) bad.fetch_add(1);
		});
	for (int i = 0; i < 20000; ++i)
		if (patch(st, pool, &r, "line " + std::to_string(i)) != PatchResult::Ok) bad.fetch_add(1);
	done.store(true, std::memory_order_release);
	for (auto& t : readers) t.join();
	CHECK(bad.load() == 0);
	CHECK(reads.load() > 0);
	CHECK(pool.freed() == 0);
	pool.release(r.slot.exchange(nullptr));
}

// Bounded cost: the cap fails closed BEFORE anything changes; repeated texts do not grow retention
// and never accumulate references.
static void test_cap_and_dedupe() {
	{
		Pool pool;
		TopicPatchState st(3);
		FakeResponse r(pool, kTopicPlaceholder);
		CHECK(patch(st, pool, &r, "a") == PatchResult::Ok);  // retains the placeholder
		CHECK(patch(st, pool, &r, "b") == PatchResult::Ok);  // retains a
		CHECK(patch(st, pool, &r, "c") == PatchResult::Ok);  // retains b
		CHECK(patch(st, pool, &r, "d") == PatchResult::RetentionFull);
		CHECK(r.text() == "c");
		CHECK(!pool.alive("d"));                             // nothing acquired on refusal
		CHECK(pool.refs("c") == 1 && pool.refs("a") == 1 && pool.refs("b") == 1);
		CHECK(pool.freed() == 0);
		CHECK(patch(st, pool, &r, std::string(151, 'x')) == PatchResult::TooLong);  // validation still first
		pool.release(r.slot.exchange(nullptr));
	}
	{
		Pool pool;
		TopicPatchState st;
		FakeResponse r(pool, kTopicPlaceholder);
		for (int i = 0; i < 1000; ++i) CHECK(patch(st, pool, &r, i % 2 ? "x" : "y") == PatchResult::Ok);
		CHECK(st.retained() == 3);                           // placeholder, x, y
		CHECK(pool.refs("x") == 2);                          // response + retention; never grows
		CHECK(pool.refs("y") == 1);                          // retention only
		CHECK(pool.freed() == 0);
		// same text twice: acquire and exchange return the same entry; still balanced
		CHECK(patch(st, pool, &r, "x") == PatchResult::Ok);
		CHECK(pool.refs("x") == 2);
		// empty replacement installs a null slot (an empty BSFixedString); the old entry is retained
		CHECK(patch(st, pool, &r, "") == PatchResult::Ok);
		CHECK(r.slot.load() == nullptr && pool.refs("x") == 1);
		CHECK(patch(st, pool, &r, "z") == PatchResult::Ok);
		pool.release(r.slot.exchange(nullptr));
	}
}

// Re-review M01 repro: recording the displaced entry fails after publication on every patch. Before
// the fix: cap 2, 8 displacements, 0 tracked. Now the first unrecordable transfer is counted, its
// reference kept, and the state sealed; nothing is ever released or freed.
static void test_unrecordable_transfer_seals() {
	Pool pool;
	TopicPatchState st(2);
	FakeResponse r(pool, kTopicPlaceholder);
	int ok = 0, refused = 0;
	for (int i = 0; i < 8; ++i) {
		const auto res = patch_response(st, &r, "line " + std::to_string(i), UnrecordableOps{ { pool }, st });
		ok += res == PatchResult::Ok;
		refused += res == PatchResult::RetentionFull;
	}
	CHECK(ok == 1 && refused == 7);
	CHECK(st.sealed());
	CHECK(st.unrecorded() == 1 && st.retained() == 0);
	CHECK(st.retained() + st.unrecorded() <= 2);       // the cap holds
	CHECK(r.text() == "line 0");
	CHECK(pool.refs(kTopicPlaceholder) == 1);          // displaced reference kept, not released
	CHECK(pool.freed() == 0);
	CHECK(balanced(pool, st, r));
	CHECK(patch(st, pool, &r, "normal ops") == PatchResult::RetentionFull);  // sealed for good
	pool.release(r.slot.exchange(nullptr));
}

// Re-review M01, reviewer's shape: arm an allocation failure AT publication on every patch. On 7d1dee0
// this gave cap 2 -> 8 displacements, 0 tracked (and an escaping bad_alloc from the record step).
// Now no allocation happens after publication at all, so the fault never fires and the cap holds.
struct ArmAtExchange : Ops {
	Entry* exchange(FakeResponse* r, Entry* e) const noexcept {
		g_fail_countdown.store(1);  // the very next allocation would throw
		return Ops::exchange(r, e);
	}
};
static void test_no_allocation_after_publication() {
	Pool pool;
	TopicPatchState st(2);
	FakeResponse r(pool, kTopicPlaceholder);
	int ok = 0, refused = 0, fired = 0;
	for (int i = 0; i < 8; ++i) {
		const auto res = patch_response(st, &r, "line " + std::to_string(i) + " long enough to defeat small strings", ArmAtExchange{ { pool } });
		fired += g_fail_countdown.exchange(0) == 0 && res == PatchResult::Ok;
		ok += res == PatchResult::Ok;
		refused += res == PatchResult::RetentionFull;
	}
	CHECK(fired == 0);                                  // nothing allocated after any publication
	CHECK(ok == 2 && refused == 6);
	CHECK(st.retained() == 2 && st.unrecorded() == 0 && !st.sealed());
	CHECK(balanced(pool, st, r));
	CHECK(pool.freed() == 0);
	pool.release(r.slot.exchange(nullptr));
}

// Re-review M01: fail exactly the k-th allocation of one patch, for every k a patch performs, both on
// the very first patch (spare node creation, placeholder displacement) and in steady state. Every
// failure must happen before publication: response and accounting unchanged, retry possible, never
// sealed, nothing freed, the cap and the reference balance hold throughout.
static void test_allocation_fault_sweep() {
	long steady = 0, first = 0;
	{
		Pool pool;
		TopicPatchState st;
		FakeResponse r(pool, kTopicPlaceholder);
		long a = g_allocs.load();
		CHECK(patch(st, pool, &r, "warm 0") == PatchResult::Ok);
		first = g_allocs.load() - a;
		a = g_allocs.load();
		CHECK(patch(st, pool, &r, "warm 1 with a text long enough to defeat any small-string buffer") == PatchResult::Ok);
		steady = g_allocs.load() - a;
		pool.release(r.slot.exchange(nullptr));
	}
	CHECK(first > 0 && steady > 0);
	int injected = 0;
	for (int warm : { 0, 2 }) {
		const long n = warm ? steady : first;
		for (long k = 1; k <= n + 2; ++k) {
			Pool pool;
			TopicPatchState st(16);
			FakeResponse r(pool, kTopicPlaceholder);
			for (int i = 0; i < warm; ++i) CHECK(patch(st, pool, &r, "warm " + std::to_string(i) + " with a text long enough to defeat any small-string buffer") == PatchResult::Ok);
			const std::string before(r.text());
			const std::size_t kept = st.retained();
			const std::string line = "faulted line " + std::to_string(k) + " with a text long enough to defeat any small-string buffer";
			g_fail_countdown.store(k);
			const auto res = patch(st, pool, &r, line);
			const bool fired = g_fail_countdown.exchange(0) == 0;
			injected += fired;
			if (fired) {
				CHECK(res == PatchResult::RetentionFull);
				CHECK(r.text() == before);                 // nothing published
				CHECK(st.retained() == kept);
				CHECK(!pool.alive(line));                  // fresh entry not leaked
			} else {
				CHECK(res == PatchResult::Ok);
			}
			CHECK(!st.sealed() && st.unrecorded() == 0);
			CHECK(balanced(pool, st, r));
			// the state keeps working afterwards, including a retry of the same response
			int displaced = static_cast<int>(st.retained());
			for (int i = 0; i < 8; ++i) {
				if (patch(st, pool, &r, "after " + std::to_string(i)) == PatchResult::Ok) ++displaced;
				CHECK(balanced(pool, st, r));
			}
			CHECK(st.retained() == static_cast<std::size_t>(displaced));
			CHECK(st.retained() <= 16);
			// The only entry that may ever be freed is the faulted patch's own, never-published one
			// (acquired, then released when recording failed). Everything ever published is alive.
			CHECK(pool.freed() <= (fired ? 1 : 0));
			CHECK(pool.alive(kTopicPlaceholder) && pool.alive(before));
			for (int i = 0; i < warm; ++i) CHECK(pool.alive("warm " + std::to_string(i) + " with a text long enough to defeat any small-string buffer"));
			for (int i = 0; i < 8; ++i) CHECK(pool.alive("after " + std::to_string(i)));
			pool.release(r.slot.exchange(nullptr));
		}
	}
	CHECK(injected >= 3);  // the sweep really hit allocations inside patch_response
}

// Re-review M02: the bounded rollover of response tracking (kMaxTracked) used to clear() the map
// BEFORE allocating the new record; an allocation failure then wiped every record and made all
// previously patched responses fail closed (-3). Fill tracking to exactly kMaxTracked, then patch one
// more response with exactly the k-th allocation failing, for every k up to the first success. Each
// failure must preserve all tracking and leave existing responses patchable; the un-faulted rollover
// then behaves as documented.
static void test_rollover_fault_preserves_tracking() {
	constexpr std::size_t N = TopicPatchState::kMaxTracked;
	const std::string pad = " with a text long enough to defeat any small-string buffer";
	Pool pool;
	TopicPatchState st;
	std::vector<std::unique_ptr<FakeResponse>> rs;
	for (std::size_t i = 0; i < N; ++i) {
		rs.push_back(std::make_unique<FakeResponse>(pool, kTopicPlaceholder));
		CHECK(patch(st, pool, rs.back().get(), "tracked " + std::to_string(i) + pad) == PatchResult::Ok);
	}
	CHECK(st.tracked() == N);
	FakeResponse extra(pool, kTopicPlaceholder);
	const std::string line = "rollover line" + pad;
	int failed = 0;
	bool rolled = false;
	for (long k = 1; k <= 256 && !rolled; ++k) {
		g_fail_countdown.store(k);
		const auto res = patch(st, pool, &extra, line);
		const bool fired = g_fail_countdown.exchange(0) == 0;
		if (!fired) {
			CHECK(res == PatchResult::Ok);
			rolled = true;
			break;
		}
		++failed;
		CHECK(res == PatchResult::RetentionFull);
		CHECK(st.tracked() == N);                          // prior tracking preserved
		CHECK(extra.text() == kTopicPlaceholder);          // nothing published
		CHECK(!pool.alive(line));
		CHECK(!st.sealed());
		for (std::size_t j : { std::size_t{ 0 }, N / 2, N - 1 })  // existing responses stay patchable
			CHECK(patch(st, pool, rs[j].get(), "repatched " + std::to_string(j) + " at fault " + std::to_string(k) + pad) == PatchResult::Ok);
		CHECK(st.tracked() == N);
	}
	CHECK(rolled);
	CHECK(failed >= 3);                                    // pool, record and rollover allocations hit
	// The rollover itself is bounded and fails closed, as documented: only the new record survives.
	CHECK(st.tracked() == 1);
	CHECK(extra.text() == line);
	CHECK(patch(st, pool, rs[0].get(), "after rollover" + pad) == PatchResult::NotMantella);
	CHECK(patch(st, pool, &extra, "extra again" + pad) == PatchResult::Ok);
	CHECK(pool.freed() <= failed);                         // only never-published entries were freed
	for (auto& r : rs) pool.release(r->slot.exchange(nullptr));
	pool.release(extra.slot.exchange(nullptr));
}

// Writer serialization (re-review N02 first half): concurrent VM-thread patches of the same and
// different responses see a consistent (placeholder or own-last) state every time.
static void test_concurrent_writers() {
	Pool pool;
	TopicPatchState cs;
	std::vector<std::unique_ptr<FakeResponse>> rs;
	for (int i = 0; i < 4; ++i) rs.push_back(std::make_unique<FakeResponse>(pool, kTopicPlaceholder));
	std::vector<std::thread> th;
	std::vector<int> ok(8, 0);
	for (int t = 0; t < 8; ++t)
		th.emplace_back([&, t] {
			for (int i = 0; i < 2000; ++i)
				if (patch(cs, pool, rs[(t + i) % 4].get(), "line " + std::to_string(t) + "-" + std::to_string(i)) == PatchResult::Ok) ++ok[t];
		});
	for (auto& x : th) x.join();
	int total = 0;
	for (int v : ok) total += v;
	CHECK(total == 16000);
	CHECK(pool.freed() == 0);
	for (auto& r : rs) pool.release(r->slot.exchange(nullptr));
}

int main() {
	test_decision_logic();
	test_raw_reader_outlives_any_window();
	test_concurrent_engine_readers();
	test_cap_and_dedupe();
	test_unrecordable_transfer_seals();
	test_no_allocation_after_publication();
	test_allocation_fault_sweep();
	test_rollover_fault_preserves_tracking();
	test_concurrent_writers();
	std::printf("test_topic_patch: %d failures\n", failures);
	return failures ? 1 : 0;
}
