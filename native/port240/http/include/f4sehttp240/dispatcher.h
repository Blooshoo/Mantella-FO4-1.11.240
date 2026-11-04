// Cancellation-aware request dispatcher for the F4SE_HTTP 1.11.240 port (Sol re-review R2-01).
//
// Original work for this port (MIT). Engine- and OS-independent: the transport is injected, so the
// scheduling contract below is exercised by tests/test_dispatcher.cpp with a blocking fake transport
// instead of WinHTTP.
//
// Problem (R2-01). Cancellation is cooperative (transport.h, re-review N01): a cancelled request that
// is already blocked inside a synchronous transport step keeps its thread until that step returns or
// times out (WinHTTP receive: up to 3 min). With a fixed pool of N workers, N quickly superseded
// requests whose server withholds the response occupy every worker and the next conversation waits
// behind them. Epochs stop a stale reply from answering it; they do not stop it from waiting.
//
// Design: SEATS, not threads.
//   * `live_workers` seats serve the current epoch. A seat is either idle or runs a job whose cancel
//     hook is registered with HttpCore for the epoch the job was built in.
//   * When HttpCore crosses a lifecycle boundary it runs the old jobs' cancel hooks (outside its own
//     lock). The hook sets the job's CancelToken and RETIRES the thread: it gives up its seat, keeps
//     running the doomed transport call, and a replacement thread takes the seat immediately - on
//     the boundary caller's thread, before prepare()/clear_all() returns. So a request enqueued after
//     a boundary never waits behind a stale transport call.
//   * Explicit bound: at most `live_workers + max_retired` threads ever exist. A retired thread whose
//     call returns either reclaims an unfilled seat or exits. If the cap prevents refilling seats and
//     no seat is left, enqueue() reports Saturated at once (the caller turns that into an error
//     reply) instead of queueing the request behind stale work. Saturation needs more than
//     `max_retired` cancelled calls blocked at the same time; each is still bounded by the
//     transport's own per-step timeout.
//   * No handle crosses threads: the transport call, and everything it owns, stays on the thread that
//     started it. The only cross-thread action is CancelToken::cancel() (an atomic flag).
//
// Lock order: dispatcher mu_ is never held while calling HttpCore or the transport; HttpCore never
// holds its lock while running cancel hooks (http_core.h), so the hook may take mu_.
#pragma once

#include "http_core.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <iterator>
#include <list>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace f4sehttp240 {

// Cooperative cancellation for one transport call. cancel() only sets a flag; the transport checks it
// between its own steps (transport_winhttp.cpp). Never closes or touches transport resources.
class CancelToken {
public:
	void cancel() noexcept { cancelled_.store(true, std::memory_order_release); }
	[[nodiscard]] bool cancelled() const noexcept { return cancelled_.load(std::memory_order_acquire); }
private:
	std::atomic<bool> cancelled_{ false };
};

struct TransportResult { int status = 0; std::string body; std::string error; };
// Must not throw for transport failures (report status 0 + error); an escaping exception becomes an
// "internal error" reply. Must not call back into the Dispatcher.
using Transport = std::function<TransportResult(const Request&, const CancelToken&)>;

inline constexpr const char* kQueueFullError = "too many pending requests";
inline constexpr const char* kSaturatedError = "transport busy with cancelled requests (try again shortly)";

struct DispatcherLimits {
	unsigned live_workers = 4;        // seats serving the current epoch (>= 1)
	unsigned max_retired = 12;        // extra threads allowed for cancelled calls that are still blocked
	std::size_t max_pending = 32;     // queued, not yet started requests
};

class Dispatcher {
public:
	enum class Enqueue { Queued, Full, Saturated };

	struct Stats {
		unsigned live = 0;           // threads holding a seat (idle or current work)
		unsigned retired = 0;        // threads still blocked in a cancelled call
		unsigned peak_threads = 0;   // max live + retired ever observed
		std::size_t pending = 0;
		std::size_t spawn_failures = 0;
	};

	Dispatcher(HttpCore& core, Transport transport, DispatcherLimits limits = {}) :
		core_(core), transport_(std::move(transport)), limits_(sanitize(limits)) {}

	Dispatcher(const Dispatcher&) = delete;
	Dispatcher& operator=(const Dispatcher&) = delete;

	// Blocks until every thread has exited, including retired ones still inside the transport.
	// The plugin never destroys its Dispatcher (process-lifetime object, see F4SE_HTTP240.cpp).
	~Dispatcher() { stop_and_join(); }

	// Spawns the seats. Idempotent. Requests queued before start() are served once it runs.
	void start() {
		std::lock_guard lk(mu_);
		if (started_ || stopping_) return;
		started_ = true;
		for (unsigned i = 0; i < limits_.live_workers; ++i) spawn_locked();
	}

	// Never blocks on transport work. Full / Saturated: nothing was queued; the caller reports an
	// error reply for the request's epoch.
	Enqueue enqueue(Request r) {
		{
			std::lock_guard lk(mu_);
			if (jobs_.size() >= limits_.max_pending) return Enqueue::Full;
			if ((started_ && live_ == 0) || stopping_) return Enqueue::Saturated;
			jobs_.push_back(std::move(r));
		}
		work_cv_.notify_one();
		return Enqueue::Queued;
	}

	[[nodiscard]] Stats stats() const {
		std::lock_guard lk(mu_);
		return Stats{ live_, retired_, peak_threads_, jobs_.size(), spawn_failures_ };
	}

	// Tests / orderly teardown only. Queued jobs are dropped; blocked transport calls are waited for.
	// No lifecycle boundary may run concurrently with (or after) teardown: a cancel hook already
	// taken out of HttpCore refers to this object.
	void stop_and_join() {
		std::unique_lock lk(mu_);
		stopping_ = true;
		jobs_.clear();
		work_cv_.notify_all();
		exit_cv_.wait(lk, [this] { return live_ + retired_ == 0; });
		// Every thread has announced its exit under mu_ and touches nothing shared afterwards.
		for (auto& t : threads_) if (t.joinable()) t.join();
		threads_.clear();
		finished_.clear();
	}

private:
	using ThreadList = std::list<std::thread>;
	struct Job {
		CancelToken token;
		bool done = false;      // guarded by mu_: the worker has left this job
		bool retired = false;   // guarded by mu_: the job's thread gave up its seat
	};

	static DispatcherLimits sanitize(DispatcherLimits l) {
		l.live_workers = std::max(1u, l.live_workers);
		l.max_pending = std::max<std::size_t>(1, l.max_pending);
		return l;
	}
	[[nodiscard]] unsigned threads_locked() const { return live_ + retired_; }
	[[nodiscard]] unsigned cap() const { return limits_.live_workers + limits_.max_retired; }

	// Adds one seated thread. Never throws (std::thread construction can).
	void spawn_locked() {
		reap_locked();
		try {
			threads_.emplace_back();
		} catch (...) {
			++spawn_failures_;
			return;
		}
		const auto it = std::prev(threads_.end());
		try {
			// The new thread blocks on mu_ (held here) before it can look at anything shared.
			*it = std::thread([this, it] { run(it); });
		} catch (...) {
			threads_.erase(it);
			++spawn_failures_;
			return;
		}
		++live_;
		peak_threads_ = std::max(peak_threads_, threads_locked());
	}

	// Joins threads that announced their exit (they hold no lock and touch no shared state after
	// announcing, so join returns promptly even under mu_).
	void reap_locked() {
		for (auto it : finished_) { if (it->joinable()) it->join(); threads_.erase(it); }
		finished_.clear();
	}

	// Cancel hook (HttpCore boundary caller's thread, outside the core lock).
	void on_cancel(const std::shared_ptr<Job>& job) {
		job->token.cancel();
		std::lock_guard lk(mu_);
		if (job->done || job->retired) return;  // already finished, or already retired
		job->retired = true;
		--live_;
		++retired_;
		if (!stopping_ && started_ && live_ < limits_.live_workers && threads_locked() < cap()) spawn_locked();
	}

	void execute(const Request& r, const std::shared_ptr<Job>& job) {
		try {
			// Register before sending; a job whose conversation already ended is dropped unsent.
			const auto id = core_.begin_inflight(r.epoch, [this, job] { on_cancel(job); });
			if (!id) return;
			struct End { HttpCore& c; std::uint64_t id; ~End() { c.end_inflight(id); } } end{ core_, id };
			TransportResult res = transport_(r, job->token);
			core_.complete(r.epoch, res.status, res.body, res.error);  // stale epochs are discarded
		} catch (...) {
			try { core_.complete(r.epoch, 0, {}, "internal error while sending request"); } catch (...) {}
		}
	}

	void run(ThreadList::iterator self) {
		std::unique_lock lk(mu_);
		bool seated = true;
		for (;;) {
			work_cv_.wait(lk, [this] { return stopping_ || !jobs_.empty(); });
			if (stopping_) break;
			Request r = std::move(jobs_.front());
			jobs_.pop_front();
			auto job = std::make_shared<Job>();
			lk.unlock();
			execute(r, job);
			lk.lock();
			job->done = true;
			if (job->retired) {
				--retired_;
				// The seat was refilled (or we are stopping): this thread's work is over.
				if (stopping_ || live_ >= limits_.live_workers) { seated = false; break; }
				++live_;  // the cap or a spawn failure left the seat empty: take it back
			}
		}
		if (seated) --live_;
		finished_.push_back(self);
		exit_cv_.notify_all();
	}

	HttpCore& core_;
	Transport transport_;
	const DispatcherLimits limits_;

	mutable std::mutex mu_;
	std::condition_variable work_cv_;
	std::condition_variable exit_cv_;
	std::deque<Request> jobs_;
	ThreadList threads_;
	std::vector<ThreadList::iterator> finished_;
	unsigned live_ = 0;
	unsigned retired_ = 0;
	unsigned peak_threads_ = 0;
	std::size_t spawn_failures_ = 0;
	bool started_ = false;
	bool stopping_ = false;
};

}  // namespace f4sehttp240
