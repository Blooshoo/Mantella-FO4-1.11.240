// Engine-independent request/reply logic of the F4SE_HTTP 1.11.240 port.
//
// Original work for this port (MIT). The Papyrus contract implemented here is the OFFICIAL Mantella
// FO4 one (staged audit copy Scripts/Source/User/F4SE_HTTP.psc + MantellaConversation.psc), which is
// NOT the same as Leidtier/F4SE_HTTP cdff839:
//   * replies are fetched by polling `int F4SE_HTTP.GetHandle()`:
//        -1                 nothing pending
//        h  (1..99999)      reply dictionary handle      -> script calls OnHttpReplyReceived(h)
//        h + 100000         error dictionary handle h    -> script calls OnHttpErrorReceived(h)
//     (cdff839 has no GetHandle and instead dispatches external Papyrus events through the
//      REL::ID(69733)/REL::ID(445184) function-argument hack, which is not used by this port.)
//   * the official scripts additionally listen for a synthetic 0x97 key press as a "data ready"
//     hint, with a 0.5 s poll timer as fallback ("sometimes signal keystroke gets lost").
//     This port does NOT synthesize that key press (no input injection); scripts fall back to their
//     poll timer, so reply latency is up to one poll period (0.5 s flat, 0.3 s in polling mode).
//
// Conversation epochs (Sol review A06). The official protocol has no request id, so the reply FIFO
// is the only correlation; a request that outlives the script's poll timeout would otherwise be
// consumed as the reply to the NEXT conversation (or its error would tear that one down). The core
// therefore keeps an epoch counter, bumped at the two lifecycle boundaries the official scripts
// define: a `mantella_request_type == "mantella_start_conversation"` request, and
// clearAllDictionaries(). A bump clears queued replies, cancels in-flight transport work of older
// epochs, drops not-yet-started requests of older epochs, and makes late completions of older
// epochs a no-op. Within one conversation the scripts are strictly request -> reply sequential.
#pragma once

#include "dict_store.h"
#include "json.h"

#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace f4sehttp240 {

inline constexpr int kNoReply = -1;
inline constexpr int kErrorOffset = 100000;
inline constexpr const char* kErrorKey = "f4se_http_error";  // upstream "F4SE_HTTP_error", lowercased by its own import
inline constexpr const char* kRequestTypeKey = "mantella_request_type";          // MantellaConstants KEY_REQUESTTYPE
inline constexpr const char* kStartConversation = "mantella_start_conversation";  // KEY_REQUESTTYPE_STARTCONVERSATION

class ReplyQueue {
public:
	explicit ReplyQueue(std::size_t capacity = 256) : capacity_(capacity ? capacity : 1) {}

	void push_reply(int handle) { push(handle); }
	void push_error(int handle) { push(handle + kErrorOffset); }

	// GetHandle(): FIFO, one entry per call.
	int pop() {
		std::lock_guard lk(mu_);
		if (q_.empty()) return kNoReply;
		int v = q_.front();
		q_.pop_front();
		return v;
	}
	void clear() { std::lock_guard lk(mu_); q_.clear(); }
	[[nodiscard]] std::size_t size() const { std::lock_guard lk(mu_); return q_.size(); }
	[[nodiscard]] std::size_t dropped() const { std::lock_guard lk(mu_); return dropped_; }

private:
	void push(int v) {
		std::lock_guard lk(mu_);
		if (q_.size() >= capacity_) { q_.pop_front(); ++dropped_; }
		q_.push_back(v);
	}
	mutable std::mutex mu_;
	std::deque<int> q_;
	std::size_t capacity_;
	std::size_t dropped_ = 0;
};

struct Request {
	std::string host = "localhost";  // upstream: always http://localhost:<port>/<route>
	int port = 0;
	std::string path;                // "/<route>" lowercased
	std::string body;                // JSON object
	int connect_timeout_ms = 0;      // 0 = transport default (upstream passed `timeout` as cpr::ConnectTimeout ms)
	std::uint64_t epoch = 0;         // conversation epoch the request belongs to
};

// Route charset is restricted so a Papyrus string can never alter the authority, query or headers.
inline bool route_ok(const std::string& r) {
	if (r.empty() || r.size() > 256) return false;
	for (unsigned char c : r) {
		if (!(std::isalnum(c) || c == '/' || c == '_' || c == '-' || c == '.')) return false;
	}
	return r.find("..") == std::string::npos;
}

class HttpCore {
public:
	using CancelFn = std::function<void()>;

	HttpCore(DictStore& store, ReplyQueue& queue, json::Limits limits = {}, DictStore::Budget budget = {}) :
		store_(store), queue_(queue), limits_(limits), budget_(budget) {}

	// sendLocalhostHttpRequest(handle, port, route, timeout). Returns the request to send, or
	// nullopt after queueing an error reply (upstream silently did nothing on bad input).
	//
	// One transaction under mu_ (Sol re-review N07): lifecycle boundary, handle validation, graph
	// snapshot and epoch capture cannot interleave with another prepare/clear, so a body is always
	// tagged with the epoch it was built in. Lock order mu_ -> store/queue (they never call back).
	// The JSON dump runs after the lock (it only touches the private snapshot).
	std::optional<Request> prepare(int handle, int port, std::string route, int timeout) {
		route = lower(std::move(route));
		while (!route.empty() && route.front() == '/') route.erase(route.begin());
		std::vector<CancelFn> cancels;
		std::optional<json::Value> body;
		Request r;
		{
			std::lock_guard lk(mu_);
			if (port < 1 || port > 65535) { error_locked("invalid port " + std::to_string(port)); return std::nullopt; }
			if (!route_ok(route)) { error_locked("invalid route"); return std::nullopt; }
			if (!store_.exists(handle)) { error_locked("unknown dictionary handle " + std::to_string(handle)); return std::nullopt; }
			if (store_.get_string(handle, kRequestTypeKey, "") == kStartConversation) {
				// New conversation: nothing from earlier conversations may answer it, and their
				// dictionaries are dead in the official scripts -> keep only this request's graph.
				cancels = bump_locked();
				store_.retain_only_reachable(handle);
			}
			body = store_.to_json(handle, budget_);
			if (!body) error_locked("request dictionary graph exceeds the size budget");
			r.epoch = epoch_;
		}
		run(cancels);  // outside the lock
		if (!body) return std::nullopt;
		r.port = port;
		r.path = "/" + route;
		r.body = json::dump(*body);
		r.connect_timeout_ms = timeout > 0 ? timeout : 0;
		return r;
	}

	// clearAllDictionaries(): also a lifecycle boundary.
	void clear_all() {
		std::vector<CancelFn> cancels;
		{
			std::lock_guard lk(mu_);
			cancels = bump_locked();
			store_.clear_all();
		}
		run(cancels);
	}

	[[nodiscard]] std::uint64_t epoch() const { std::lock_guard lk(mu_); return epoch_; }
	[[nodiscard]] bool current(std::uint64_t e) const { return e == epoch(); }

	// Transport bookkeeping: register a cancel hook for an in-flight request; returns 0 (and does
	// not register) if the request's epoch is already stale - the caller must then not send it.
	std::uint64_t begin_inflight(std::uint64_t e, CancelFn cancel) {
		std::lock_guard lk(mu_);
		if (e != epoch_) return 0;
		const std::uint64_t id = ++next_inflight_;
		inflight_.emplace(id, Inflight{ e, std::move(cancel) });
		return id;
	}
	void end_inflight(std::uint64_t id) { std::lock_guard lk(mu_); inflight_.erase(id); }
	[[nodiscard]] std::size_t inflight() const { std::lock_guard lk(mu_); return inflight_.size(); }

	// Transport completion (any thread). status 0 = transport failure with `transport_error`.
	// Completions of an older epoch are discarded (they would answer the wrong conversation).
	void complete(std::uint64_t e, int status, const std::string& body, const std::string& transport_error) {
		std::optional<json::Value> v;
		std::string why;
		if (status == 200) v = json::parse(body, limits_, &why);  // parse outside the lock
		std::lock_guard lk(mu_);
		if (e != epoch_) { ++stale_; return; }
		if (status != 200) {
			error_locked(transport_error.empty() ? "HTTP status " + std::to_string(status) : transport_error);
			return;
		}
		if (!v || v->type != json::Type::Object) {
			error_locked(v ? std::string("reply is not a JSON object") : "invalid JSON reply: " + why);
			return;
		}
		int h = store_.from_json(*v);
		if (h <= 0) { error_locked("could not store reply (dictionary store full or reply too large)"); return; }
		queue_.push_reply(h);
	}
	// Convenience for a completion in the current epoch (tests, synchronous rejects).
	void complete(int status, const std::string& body, const std::string& transport_error) {
		complete(epoch(), status, body, transport_error);
	}

	int get_handle() { return queue_.pop(); }
	[[nodiscard]] std::size_t stale_discarded() const { std::lock_guard lk(mu_); return stale_; }

private:
	struct Inflight { std::uint64_t epoch; CancelFn cancel; };

	// Returns the cancel hooks of older in-flight work; callers run them AFTER releasing mu_, so a
	// hook can never deadlock against the core or block the Papyrus thread under the lock.
	[[nodiscard]] std::vector<CancelFn> bump_locked() {
		++epoch_;
		queue_.clear();
		std::vector<CancelFn> out;
		for (auto it = inflight_.begin(); it != inflight_.end();) {
			if (it->second.epoch != epoch_) {
				if (it->second.cancel) out.push_back(std::move(it->second.cancel));
				it = inflight_.erase(it);
			} else {
				++it;
			}
		}
		return out;
	}
	static void run(std::vector<CancelFn>& fns) { for (auto& f : fns) f(); }

	void error(const std::string& msg) { std::lock_guard lk(mu_); error_locked(msg); }
	void error_locked(const std::string& msg) {
		int h = store_.create(/*for_error=*/true);
		if (h <= 0) return;
		store_.set_error(h, kErrorKey, msg);
		queue_.push_error(h);
	}

	DictStore& store_;
	ReplyQueue& queue_;
	json::Limits limits_;
	DictStore::Budget budget_;
	mutable std::mutex mu_;  // order: mu_ -> store/queue mutexes (they never call back)
	std::uint64_t epoch_ = 1;
	std::uint64_t next_inflight_ = 0;
	std::map<std::uint64_t, Inflight> inflight_;
	std::size_t stale_ = 0;
};

}  // namespace f4sehttp240
