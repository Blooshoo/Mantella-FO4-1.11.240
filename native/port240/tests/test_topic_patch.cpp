// PatchTopicInfo decision logic (Sol review A02/A03) against a fake pooled-string model.
// A response holds a pointer to a shared, interned, immutable entry - like BSFixedStringCS.
#include "mantella240/topic_patch.h"

#include <cstdio>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)

using namespace mantella240;

struct Pool {
	std::mutex mu;
	std::map<std::string, std::shared_ptr<const std::string>> entries;
	std::shared_ptr<const std::string> intern(std::string_view s) {
		std::lock_guard lk(mu);
		auto& e = entries[std::string(s)];
		if (!e) e = std::make_shared<const std::string>(s);
		return e;
	}
};
struct FakeResponse { std::shared_ptr<const std::string> text; };

static PatchResult patch(TopicPatchState& st, Pool& pool, FakeResponse* r, std::string_view t) {
	return patch_response(st, r, t,
		[](FakeResponse* x) { return std::string_view(*x->text); },
		[&](FakeResponse* x, std::string_view s) { x->text = pool.intern(s); });  // never mutates an entry
}

int main() {
	Pool pool;
	TopicPatchState st;

	// A02: 149 / 150 / 151 bytes. Upstream accepted 150 and then strcpy_s(dst,150,...) aborted.
	for (std::size_t n : { 149u, 150u }) {
		FakeResponse r{ pool.intern(kTopicPlaceholder) };
		CHECK(patch(st, pool, &r, std::string(n, 'A')) == PatchResult::Ok);
		CHECK(*r.text == std::string(n, 'A'));
	}
	{
		FakeResponse r{ pool.intern(kTopicPlaceholder) };
		CHECK(patch(st, pool, &r, std::string(151, 'A')) == PatchResult::TooLong);
		CHECK(*r.text == kTopicPlaceholder);
	}

	// A03: two Mantella topics share ONE pooled placeholder entry. Patching one must not change the other.
	FakeResponse t1{ pool.intern(kTopicPlaceholder) }, t2{ pool.intern(kTopicPlaceholder) };
	CHECK(t1.text == t2.text);
	CHECK(patch(st, pool, &t1, "Hi") == PatchResult::Ok);
	CHECK(*t1.text == "Hi");
	CHECK(*t2.text == kTopicPlaceholder);
	CHECK(*pool.intern(kTopicPlaceholder) == kTopicPlaceholder);  // the pool entry itself is untouched

	// A03: an unrelated single-response topic whose text happens to equal the last subtitle ("Hi")
	// passed upstream's global lastText check and got a 151-byte memset. Must be rejected.
	FakeResponse other{ pool.intern("Hi") };
	CHECK(patch(st, pool, &other, "Overwrite") == PatchResult::NotMantella);
	CHECK(*other.text == "Hi");

	// The patched Mantella response can be patched again (per-response bookkeeping) ...
	CHECK(patch(st, pool, &t1, "Second line") == PatchResult::Ok);
	CHECK(*t1.text == "Second line");
	// ... but not if something else changed its text since.
	t1.text = pool.intern("changed by someone else");
	CHECK(patch(st, pool, &t1, "Third") == PatchResult::NotMantella);
	// a reloaded form shows the placeholder again and is patchable
	t1.text = pool.intern(kTopicPlaceholder);
	CHECK(patch(st, pool, &t1, "After reload") == PatchResult::Ok);

	// null response
	CHECK(patch_response(st, static_cast<FakeResponse*>(nullptr), "x", [](FakeResponse*) { return std::string_view{}; },
		[](FakeResponse*, std::string_view) {}) == PatchResult::BadRecord);

	// N02: concurrent VM-thread patches of the same and different responses. The fake response's
	// shared_ptr member is NOT thread-safe; only patch_response's single critical section keeps
	// this race-free (run_tests.sh builds this test under TSan as well).
	{
		TopicPatchState cs;
		std::vector<FakeResponse> rs(4);
		for (auto& r : rs) r.text = pool.intern(kTopicPlaceholder);
		std::vector<std::thread> th;
		std::vector<int> ok(8, 0);
		for (int t = 0; t < 8; ++t)
			th.emplace_back([&, t] {
				for (int i = 0; i < 2000; ++i)
					if (patch(cs, pool, &rs[(t + i) % 4], "line " + std::to_string(t) + "-" + std::to_string(i)) == PatchResult::Ok) ++ok[t];
			});
		for (auto& x : th) x.join();
		int total = 0;
		for (int v : ok) total += v;
		CHECK(total == 16000);  // every patch saw a consistent (placeholder or own-last) state
	}

	std::printf("test_topic_patch: %d failures (tracked %zu)\n", failures, st.tracked());
	return failures ? 1 : 0;
}
