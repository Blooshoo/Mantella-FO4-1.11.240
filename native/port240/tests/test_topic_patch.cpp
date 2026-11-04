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
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)

using namespace mantella240;

struct Entry {
	std::atomic<int> refs{ 0 };
	std::string text;
};

class Pool {
public:
	~Pool() { for (auto& [k, e] : live_) delete e; }
	// GetEntry: one new reference (nullptr for "", like an empty BSFixedString).
	Entry* acquire(std::string_view s) {
		if (s.empty()) return nullptr;
		std::lock_guard lk(mu_);
		auto& e = live_[std::string(s)];
		if (!e) { e = new Entry; e->text = std::string(s); }
		e->refs.fetch_add(1, std::memory_order_relaxed);
		return e;
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
	test_concurrent_writers();
	std::printf("test_topic_patch: %d failures\n", failures);
	return failures ? 1 : 0;
}
