// Deterministic regressions for the cancellation-aware dispatcher (Sol re-review R2-01).
//
// The fake transport blocks every call on a per-request barrier and IGNORES its CancelToken: it models
// the worst WinHTTP case, a synchronous call stuck inside WinHttpReceiveResponse until its 3-minute
// timeout. Old barriers are never released before the availability assertions, so a pass cannot come
// from a stale call happening to return. Each wait has a watchdog (kBudget) only so a regression
// fails instead of hanging; no assertion depends on timing luck.
#include "f4sehttp240/dispatcher.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

static int failures = 0;
static int checks = 0;
#define CHECK(c) do { ++checks; if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)

using namespace f4sehttp240;
using namespace std::chrono_literals;

// Generous for sanitizer builds; a correct dispatcher starts a request in microseconds.
static constexpr auto kBudget = 5s;

class BlockingTransport {
public:
	TransportResult call(const Request& r, const CancelToken& token) {
		auto v = json::parse(r.body);
		std::string tag;
		if (v && v->type == json::Type::Object)
			for (const auto& [k, val] : v->obj) if (k == "tag") tag = val.s;
		std::unique_lock lk(mu_);
		started_.push_back(tag);
		tokens_[tag] = &token;  // valid while the call is blocked
		++active_;
		peak_active_ = std::max(peak_active_, active_);
		cv_.notify_all();
		cv_.wait(lk, [&] { return all_ || released_.contains(tag); });  // token deliberately ignored
		tokens_.erase(tag);
		--active_;
		returned_.insert(tag);
		cv_.notify_all();
		return { 200, R"({"from":")" + tag + R"("})", {} };
	}
	bool wait_started(const std::string& tag) {
		std::unique_lock lk(mu_);
		return cv_.wait_for(lk, kBudget, [&] { return std::find(started_.begin(), started_.end(), tag) != started_.end(); });
	}
	bool wait_returned(const std::string& tag) {
		std::unique_lock lk(mu_);
		return cv_.wait_for(lk, kBudget, [&] { return returned_.contains(tag); });
	}
	void release(const std::string& tag) { std::lock_guard lk(mu_); released_.insert(tag); cv_.notify_all(); }
	void release_all() { std::lock_guard lk(mu_); all_ = true; cv_.notify_all(); }
	bool any_released() { std::lock_guard lk(mu_); return all_ || !released_.empty() || !returned_.empty(); }
	bool started(const std::string& tag) { std::lock_guard lk(mu_); return std::find(started_.begin(), started_.end(), tag) != started_.end(); }
	// -1 unknown/not blocked, else cancelled()
	int cancelled(const std::string& tag) {
		std::lock_guard lk(mu_);
		auto it = tokens_.find(tag);
		return it == tokens_.end() ? -1 : (it->second->cancelled() ? 1 : 0);
	}
	int active() { std::lock_guard lk(mu_); return active_; }
	int peak_active() { std::lock_guard lk(mu_); return peak_active_; }

private:
	std::mutex mu_;
	std::condition_variable cv_;
	std::vector<std::string> started_;
	std::set<std::string> released_, returned_;
	std::map<std::string, const CancelToken*> tokens_;
	bool all_ = false;
	int active_ = 0, peak_active_ = 0;
};

struct Rig {
	DictStore store;
	ReplyQueue queue;
	HttpCore core{ store, queue };
	BlockingTransport fake;
	Dispatcher disp;
	explicit Rig(DispatcherLimits l) :
		disp(core, [this](const Request& r, const CancelToken& t) { return fake.call(r, t); }, l) { disp.start(); }
	~Rig() { fake.release_all(); disp.stop_and_join(); }

	// What sendLocalhostHttpRequest does: prepare (may cross a boundary), then enqueue.
	Dispatcher::Enqueue send(const std::string& tag, bool start) {
		int h = store.create();
		if (start) store.set_string(h, kRequestTypeKey, kStartConversation);
		else store.set_string(h, kRequestTypeKey, "mantella_continue_conversation");
		store.set_string(h, "tag", tag);
		auto r = core.prepare(h, 4999, "mantella", 0);
		if (!r) return Dispatcher::Enqueue::Full;
		const auto epoch = r->epoch;
		auto res = disp.enqueue(std::move(*r));
		if (res == Dispatcher::Enqueue::Full) core.complete(epoch, 0, {}, kQueueFullError);
		if (res == Dispatcher::Enqueue::Saturated) core.complete(epoch, 0, {}, kSaturatedError);
		return res;
	}
	// Polls GetHandle like the scripts do; returns the first handle or kNoReply after kBudget.
	int poll() {
		const auto until = std::chrono::steady_clock::now() + kBudget;
		while (std::chrono::steady_clock::now() < until) {
			int g = core.get_handle();
			if (g != kNoReply) return g;
			std::this_thread::sleep_for(1ms);
		}
		return kNoReply;
	}
	bool wait_stats(auto pred) {
		const auto until = std::chrono::steady_clock::now() + kBudget;
		while (std::chrono::steady_clock::now() < until) {
			if (pred(disp.stats())) return true;
			std::this_thread::sleep_for(1ms);
		}
		return pred(disp.stats());
	}
};

// R2-01 reviewer repro: four quickly superseded conversations, each server withholding its response,
// occupy all four original workers; the fifth conversation must start while all four stay blocked.
static void test_superseded_conversations_do_not_starve_next() {
	DispatcherLimits l;  // production defaults: 4 seats, 12 retired
	Rig rig(l);
	for (int i = 0; i < 4; ++i) {
		const std::string tag = "conv" + std::to_string(i);
		CHECK(rig.send(tag, true) == Dispatcher::Enqueue::Queued);
		CHECK(rig.fake.wait_started(tag));
	}
	CHECK(rig.fake.active() == 4);
	// The fifth start crosses a boundary; it must not wait for any stale call to return.
	CHECK(rig.send("conv4", true) == Dispatcher::Enqueue::Queued);
	CHECK(rig.fake.wait_started("conv4"));
	CHECK(!rig.fake.any_released());                  // no old barrier was released
	CHECK(rig.fake.active() == 5);                    // all four stale calls still blocked
	for (int i = 0; i < 4; ++i) CHECK(rig.fake.cancelled("conv" + std::to_string(i)) == 1);
	CHECK(rig.fake.cancelled("conv4") == 0);
	auto st = rig.disp.stats();
	CHECK(st.retired == 4 && st.live == 4);
	CHECK(st.peak_threads <= l.live_workers + l.max_retired);

	// The new conversation gets its own reply; nothing stale is ever delivered.
	rig.fake.release("conv4");
	int g = rig.poll();
	CHECK(g > 0 && g < kErrorOffset && rig.store.get_string(g, "from", "") == "conv4");
	for (int i = 0; i < 4; ++i) rig.fake.release("conv" + std::to_string(i));
	CHECK(rig.wait_stats([](const Dispatcher::Stats& s) { return s.retired == 0; }));
	CHECK(rig.core.get_handle() == kNoReply);
	CHECK(rig.core.stale_discarded() == 4);
	st = rig.disp.stats();
	CHECK(st.live == l.live_workers);                 // retired threads exited; seats were refilled
}

// Every seat is busy with blocked current-epoch work; clearAllDictionaries (a boundary) retires them
// all and the next conversation still starts at once.
static void test_clear_all_frees_every_seat() {
	DispatcherLimits l;
	l.live_workers = 4;
	Rig rig(l);
	for (int i = 0; i < 4; ++i) {
		const std::string tag = "busy" + std::to_string(i);
		CHECK(rig.send(tag, false) == Dispatcher::Enqueue::Queued);
		CHECK(rig.fake.wait_started(tag));
	}
	// A fifth current-epoch request legitimately queues behind current work (no boundary).
	CHECK(rig.send("queued", false) == Dispatcher::Enqueue::Queued);
	CHECK(rig.disp.stats().pending == 1);
	rig.core.clear_all();
	CHECK(rig.send("next", true) == Dispatcher::Enqueue::Queued);
	CHECK(rig.fake.wait_started("next"));
	CHECK(!rig.fake.any_released());
	CHECK(!rig.fake.started("queued"));               // stale queued job dropped unsent
	CHECK(rig.wait_stats([](const Dispatcher::Stats& s) { return s.pending == 0; }));
	rig.fake.release("next");
	int g = rig.poll();
	CHECK(g > 0 && g < kErrorOffset && rig.store.get_string(g, "from", "") == "next");
}

// The thread bound is explicit. When it prevents refilling seats and none is left, a new request is
// rejected at once with an error reply (never queued behind stale work); a returning stale thread
// reclaims an empty seat and service resumes.
static void test_cap_fails_fast_then_recovers() {
	DispatcherLimits l;
	l.live_workers = 2;
	l.max_retired = 2;  // at most 4 threads
	Rig rig(l);
	for (int i = 0; i < 4; ++i) {
		const std::string tag = "c" + std::to_string(i);
		CHECK(rig.send(tag, true) == Dispatcher::Enqueue::Queued);
		CHECK(rig.fake.wait_started(tag));
	}
	// c0..c2 retired (4 threads = cap), c3 runs on the last seat.
	auto st = rig.disp.stats();
	CHECK(st.retired == 3 && st.live == 1);
	// c4's boundary retires c3 with no replacement: no seat is left -> immediate error, not a stall.
	CHECK(rig.send("c4", true) == Dispatcher::Enqueue::Saturated);
	int g = rig.core.get_handle();
	CHECK(g > kErrorOffset);
	CHECK(g > kErrorOffset && rig.store.get_string(g - kErrorOffset, kErrorKey, "") == kSaturatedError);
	CHECK(!rig.fake.started("c4"));
	st = rig.disp.stats();
	CHECK(st.live == 0 && st.retired == 4 && st.peak_threads == 4);
	CHECK(rig.fake.peak_active() == 4);
	// One stale call returns: its thread takes the empty seat back; the next conversation is served.
	rig.fake.release("c0");
	CHECK(rig.fake.wait_returned("c0"));
	CHECK(rig.wait_stats([](const Dispatcher::Stats& s) { return s.live == 1 && s.retired == 3; }));
	CHECK(rig.send("c5", true) == Dispatcher::Enqueue::Queued);
	CHECK(rig.fake.wait_started("c5"));
	rig.fake.release("c5");
	g = rig.poll();
	CHECK(g > 0 && g < kErrorOffset && rig.store.get_string(g, "from", "") == "c5");
	CHECK(rig.disp.stats().peak_threads <= 4);
}

// A boundary with nothing in flight retires nothing; the pending-queue bound still answers with an
// error reply instead of silently dropping the request.
static void test_idle_boundary_and_queue_limits() {
	DispatcherLimits l;
	l.live_workers = 1;
	l.max_retired = 1;
	l.max_pending = 2;
	Rig rig(l);
	rig.fake.release("a");
	CHECK(rig.send("a", true) == Dispatcher::Enqueue::Queued);
	CHECK(rig.fake.wait_returned("a"));
	int g = rig.poll();
	CHECK(g > 0 && g < kErrorOffset);
	CHECK(rig.wait_stats([](const Dispatcher::Stats& s) { return s.live == 1 && s.retired == 0; }));
	rig.core.clear_all();  // nothing in flight: no retirement, no extra thread
	auto st = rig.disp.stats();
	CHECK(st.live == 1 && st.retired == 0 && st.peak_threads == 1);
	// Queue bound: one running + two pending; the fourth is refused with an error reply.
	CHECK(rig.send("b", false) == Dispatcher::Enqueue::Queued);
	CHECK(rig.fake.wait_started("b"));
	CHECK(rig.send("q1", false) == Dispatcher::Enqueue::Queued);
	CHECK(rig.send("q2", false) == Dispatcher::Enqueue::Queued);
	CHECK(rig.send("q3", false) == Dispatcher::Enqueue::Full);
	g = rig.core.get_handle();
	CHECK(g > kErrorOffset && rig.store.get_string(g - kErrorOffset, kErrorKey, "") == kQueueFullError);
}

// Not started yet: requests queue and are served on start(); stop_and_join waits for blocked calls.
static void test_queue_before_start() {
	DictStore store;
	ReplyQueue queue;
	HttpCore core(store, queue);
	BlockingTransport fake;
	fake.release("early");
	{
		Dispatcher d(core, [&](const Request& r, const CancelToken& t) { return fake.call(r, t); }, {});
		int h = store.create();
		store.set_string(h, "tag", "early");
		auto r = core.prepare(h, 4999, "mantella", 0);
		CHECK(r.has_value());
		CHECK(d.enqueue(std::move(*r)) == Dispatcher::Enqueue::Queued);
		CHECK(d.stats().live == 0 && d.stats().pending == 1);
		d.start();
		CHECK(fake.wait_returned("early"));
	}  // destructor joins
	CHECK(fake.active() == 0);
}

// Transport exceptions become an error reply for the request's epoch and keep the seat.
static void test_transport_exception() {
	DictStore store;
	ReplyQueue queue;
	HttpCore core(store, queue);
	Dispatcher d(core, [](const Request&, const CancelToken&) -> TransportResult { throw 1; }, { 1, 0, 4 });
	d.start();
	int h = store.create();
	auto r = core.prepare(h, 4999, "mantella", 0);
	CHECK(r.has_value() && d.enqueue(std::move(*r)) == Dispatcher::Enqueue::Queued);
	int g = kNoReply;
	const auto until = std::chrono::steady_clock::now() + kBudget;
	while (g == kNoReply && std::chrono::steady_clock::now() < until) { g = core.get_handle(); std::this_thread::sleep_for(1ms); }
	CHECK(g > kErrorOffset && store.get_string(g - kErrorOffset, kErrorKey, "").find("internal error") != std::string::npos);
	CHECK(d.stats().live == 1);
}

int main() {
	test_superseded_conversations_do_not_starve_next();
	test_clear_all_frees_every_seat();
	test_cap_fails_fast_then_recovers();
	test_idle_boundary_and_queue_limits();
	test_queue_before_start();
	test_transport_exception();
	std::printf("test_dispatcher: %d checks, %d failures\n", checks, failures);
	return failures ? 1 : 0;
}
