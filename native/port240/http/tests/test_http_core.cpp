// Unit tests for the engine-independent F4SE_HTTP 1.11.240 core (json, dict store, reply queue,
// request preparation) and the Papyrus contract table vs the official Mantella F4SE_HTTP.psc.
//
// Env (optional): F4SEHTTP240_OFFICIAL_PSC  official staged F4SE_HTTP.psc (read-only)
//                 F4SEHTTP240_UPSTREAM_PSC  upstream cdff839 F4SE_HTTP.psc (read-only)
#include "f4sehttp240/contract.h"
#include "f4sehttp240/http_core.h"

#include <algorithm>
#include <atomic>
#include <mutex>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <thread>

static int failures = 0;
static int checks = 0;
#define CHECK(c) do { ++checks; if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)
// loop-body variant: counts once per failing line, not per iteration
#define CHECK_QUIET(c) do { if (!(c)) { static bool once = false; if (!once) { once = true; std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); } ++failures; } } while (0)

using namespace f4sehttp240;

// ------------------------------------------------------------------------------------------- json
static void test_json() {
	auto v = json::parse(R"({"a":1,"b":1.5,"c":"x\u00e9\ud83d\ude00","d":[true,null],"e":{"f":-2e3}})");
	CHECK(v && v->type == json::Type::Object && v->obj.size() == 5);
	CHECK(v->obj[0].second.type == json::Type::Int && v->obj[0].second.i == 1);
	CHECK(v->obj[1].second.type == json::Type::Float && v->obj[1].second.f == 1.5);
	CHECK(v->obj[2].second.s == "x\xC3\xA9\xF0\x9F\x98\x80");
	CHECK(v->obj[4].second.obj[0].second.type == json::Type::Float);  // exponent => float

	CHECK(!json::parse("{"));
	CHECK(!json::parse("{} x"));
	CHECK(!json::parse("[1,]"));
	CHECK(!json::parse("01"));
	CHECK(!json::parse("\"\x01\""));
	CHECK(!json::parse("\"\\ud800\""));          // lone surrogate
	CHECK(!json::parse("\"\xC3\x28\""));         // invalid UTF-8
	CHECK(!json::parse("\"\xC0\xAF\""));         // overlong

	std::string deep(65, '['); deep += std::string(65, ']');
	CHECK(!json::parse(deep));                   // depth limit 64
	std::string ok64(64, '['); ok64 += std::string(64, ']');
	CHECK(json::parse(ok64).has_value());
	json::Limits tiny; tiny.max_bytes = 4;
	CHECK(!json::parse("[1,2]", tiny));
	json::Limits few; few.max_nodes = 3;
	CHECK(!json::parse("[1,2,3]", few));

	CHECK(json::dump(json::Value::number(0.5)) == "0.5");
	CHECK(json::dump(json::Value::number(2.0)) == "2.0");
	CHECK(json::dump(json::Value::number(std::nan(""))) == "null");
	CHECK(json::dump(json::Value::string("a\"b\n\x01")) == "\"a\\\"b\\n\\u0001\"");
	CHECK(json::dump(json::Value::string("\xFFok")) == "\"\xEF\xBF\xBDok\"");  // never throws
	auto big = json::parse("123456789012345678901234567890");
	CHECK(big && big->type == json::Type::Float);
	CHECK(!json::parse("1e999"));                 // overflow is an error (nlohmann 406), never inf
	auto neg = json::parse("-9223372036854775808");
	CHECK(neg && neg->type == json::Type::Int && neg->i == INT64_MIN);
	CHECK(json::dump(json::Value::number(1e20)).find_first_of(".eE") != std::string::npos);
	// round trip
	auto rt = json::parse(json::dump(*v));
	CHECK(rt && json::dump(*rt) == json::dump(*v));
}

// ------------------------------------------------------------------------------------- dict store
static void test_store_basics() {
	DictStore s;
	int h = s.create();
	CHECK(h == 1);                               // never 0 (0 is getNestedDictionary's default)
	CHECK(s.set(h, "Key", std::int32_t{ 7 }));   // keys lowercased
	CHECK(s.get_int(h, "key", 0) == 7);
	CHECK(s.get_int(h, "KEY", 0) == 7);
	CHECK(s.has_key(h, "KEY"));                  // D5: upstream hasKey did not lowercase
	CHECK(!s.has_key(h, "nope"));
	CHECK(!s.set(4242, "k", std::int32_t{ 1 })); // unknown handle
	CHECK(s.get_string(4242, "k", "dflt") == "dflt");
	CHECK(s.get_int_array(4242, "k").empty());

	// D1: type-checked getters with documented coercions
	s.set(h, "i", std::int32_t{ 3 });
	s.set(h, "f", 2.75f);
	s.set(h, "b", true);
	s.set(h, "s", std::string("text"));
	CHECK(s.get_float(h, "i", -1) == 3.0f);
	CHECK(s.get_int(h, "f", -1) == 2);
	s.set(h, "nf", -2.75f);
	CHECK(s.get_int(h, "nf", 0) == -2);          // truncation toward zero
	s.set(h, "huge", 1e20f);
	CHECK(s.get_int(h, "huge", 0) == INT32_MAX); // saturated
	CHECK(s.get_int(h, "b", -1) == 1);
	CHECK(s.get_bool(h, "i", false));
	CHECK(s.get_string(h, "i", "d") == "d");     // upstream: garbage read / crash
	CHECK(s.get_int(h, "s", 9) == 9);
	CHECK(s.get_nested(h, "i", 0) == 0);
	CHECK(s.get_string_array(h, "s").empty());

	s.set(h, "ia", std::vector<std::int32_t>{ 1, 2 });
	s.set(h, "fa", std::vector<float>{ 1.9f, -1.9f });
	CHECK((s.get_float_array(h, "ia") == std::vector<float>{ 1.0f, 2.0f }));
	CHECK((s.get_int_array(h, "fa") == std::vector<std::int32_t>{ 1, -1 }));
	CHECK(s.get_bool_array(h, "ia").empty());

	CHECK(!s.set_string(h, "bad", std::string("\xC3\x28")));   // upstream setString UTF-8 rule
	CHECK(!s.has_key(h, "bad"));
	CHECK(!s.set_string_array(h, "sa", { "ok", "\xFF", "fine" }));
	CHECK((s.get_string_array(h, "sa") == std::vector<std::string>{ "ok", "fine" }));
	CHECK(*s.value_type_name(h, "sa") == std::string("stringArray"));

	s.clear_all();
	CHECK(s.size() == 0);
	CHECK(!s.exists(h));
}

static void test_store_handle_space() {
	// D3: handles stay inside [1, 99999] forever, so GetHandle's +100000 error encoding is unambiguous,
	// also across many clear_all() lifecycle reclaims.
	DictStore s(1000);
	int minH = 1 << 30, maxH = 0;
	for (int i = 0; i < 150000; ++i) {
		if (i % 1000 == 0) s.clear_all();
		int h = s.create();
		CHECK_QUIET(h > 0);
		minH = std::min(minH, h);
		maxH = std::max(maxH, h);
	}
	CHECK(minH >= DictStore::kFirstHandle);
	CHECK(maxH <= DictStore::kLastHandle);
	CHECK(s.size() == 1000);
}

// Sol review A05 repro: `DictStore s(2); old=create(); 99,999 more creates` used to evict `old` and
// reissue its number, so reads through the stale handle returned an unrelated dictionary.
static void test_store_no_stale_alias() {
	DictStore s(2);
	int old = s.create();
	s.set(old, "who", std::string("old"));
	int other = s.create();
	CHECK(other > 0 && other != old);
	for (int i = 0; i < 99999; ++i) CHECK_QUIET(s.create() == 0);  // full: fail closed, nothing evicted
	CHECK(s.exists(old));
	CHECK(s.get_string(old, "who", "") == "old");
	CHECK(s.rejected() >= 99999);
	// a reclaimed number is not reissued before the counter has cycled the whole range
	DictStore t(10);
	int a = t.create();
	t.clear_all();
	int b = t.create();
	CHECK(b != a);
	// queued reply handles survive later creates (old store evicted them at capacity)
	DictStore u(4);
	ReplyQueue q;
	HttpCore core(u, q);
	core.complete(200, R"({"mantella_replytype":"x"})", "");
	for (int i = 0; i < 4; ++i) u.create();
	int got = core.get_handle();
	CHECK(got > 0 && got < kErrorOffset);
	CHECK(u.get_string(got, "mantella_replytype", "") == "x");
}

// Store byte budget: a huge value fails closed instead of growing without bound.
static void test_store_byte_budget() {
	DictStore s(100, 1u << 16);
	int h = s.create();
	CHECK(!s.set(h, "big", std::string(1u << 17, 'a')));
	CHECK(!s.has_key(h, "big"));
	CHECK(s.set(h, "small", std::string(100, 'a')));
	CHECK(s.set(h, "small", std::string(10, 'b')));   // replacing returns bytes
	const auto before = s.retained_bytes();
	CHECK(s.set(h, "small", std::string(10, 'c')));
	CHECK(s.retained_bytes() == before);
	auto reply = json::parse("{\"a\":\"" + std::string(1u << 15, 'x') + "\",\"b\":\"" + std::string(1u << 15, 'y') + "\"}");
	CHECK(reply.has_value());
	const auto n = s.size();
	CHECK(s.from_json(*reply) == 0);                  // over budget -> rolled back, nothing left behind
	CHECK(s.size() == n);
}

// Sol review A04 repro: h[i].k = [h[i+1], h[i+1]] for i < 32 expands to 2^32 nodes. Must be rejected
// within the budget (this test would not finish against the old unbudgeted to_json).
static void test_request_expansion_budget() {
	DictStore s;
	ReplyQueue q;
	HttpCore core(s, q);
	std::vector<int> h(33);
	for (auto& x : h) x = s.create();
	s.set(h[32], "v", std::int32_t{ 1 });
	for (int i = 0; i < 32; ++i) s.set(h[i], "k", NestedArray{ { h[i + 1], h[i + 1] } });
	const auto t0 = std::chrono::steady_clock::now();
	CHECK(!s.to_json(h[0]).has_value());
	CHECK(!core.prepare(h[0], 4999, "mantella", 0).has_value());
	const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count();
	CHECK(ms < 5000);
	int g = core.get_handle();
	CHECK(g > kErrorOffset);
	CHECK(s.get_string(g - kErrorOffset, kErrorKey, "").find("size budget") != std::string::npos);
	// the same shape at depth 10 (2^10 leaves) is within budget and still serialises
	CHECK(s.to_json(h[22]).has_value());
	// a big string repeated through a DAG hits the byte budget
	DictStore t;
	int leaf = t.create();
	t.set(leaf, "s", std::string(1u << 20, 'z'));
	int root = t.create();
	t.set(root, "k", NestedArray{ std::vector<int>(16, leaf) });
	CHECK(!t.to_json(root).has_value());
}

// Sol review A06 repro: a reply of conversation A that arrives after B started must not be consumed
// by B, and A's in-flight work is cancelled at the boundary.
// Sol re-review N07: before the fix, prepare() bumped the epoch, released the core lock, built the
// body and then read the CURRENT epoch, so two concurrent starts could both be tagged with the
// later epoch (the older body would then be accepted as the newer conversation). Each start must
// get its own epoch, and the cancel hook of an older request must run outside the core lock.
static void test_concurrent_starts_atomic_epoch() {
	DictStore s(50000);
	ReplyQueue q;
	HttpCore core(s, q);
	std::mutex mu;
	std::vector<std::uint64_t> epochs;
	auto worker = [&] {
		for (int i = 0; i < 400; ++i) {
			int h = s.create();
			if (h <= 0) continue;  // a concurrent start may have reclaimed it already
			if (!s.set_string(h, kRequestTypeKey, kStartConversation)) continue;
			auto r = core.prepare(h, 4999, "mantella", 0);
			if (!r) { (void)core.get_handle(); continue; }
			// the hook re-enters the core: deadlocks if hooks ran under mu_
			core.begin_inflight(r->epoch, [&core] { (void)core.epoch(); });
			std::lock_guard lk(mu);
			epochs.push_back(r->epoch);
		}
	};
	std::thread a(worker), b(worker);
	a.join(); b.join();
	std::sort(epochs.begin(), epochs.end());
	CHECK(!epochs.empty());
	CHECK(std::adjacent_find(epochs.begin(), epochs.end()) == epochs.end());
}

static void test_conversation_epochs() {
	DictStore s;
	ReplyQueue q;
	HttpCore core(s, q);
	auto start = [&] {
		int h = s.create();
		s.set_string(h, kRequestTypeKey, kStartConversation);
		int ctx = s.create();
		s.set(ctx, "location", std::string("Sanctuary"));
		s.set(h, "context", NestedRef{ ctx });
		return std::pair{ h, ctx };
	};
	auto [ha, ctxa] = start();
	auto ra = core.prepare(ha, 4999, "mantella", 0);
	CHECK(ra.has_value());
	int cancelled = 0;
	auto ida = core.begin_inflight(ra->epoch, [&] { ++cancelled; });
	CHECK(ida != 0);
	// A's reply is still pending when the script times out and conversation B starts
	int leftover = s.create();          // some dictionary of conversation A
	auto [hb, ctxb] = start();
	auto rb = core.prepare(hb, 4999, "mantella", 0);
	CHECK(rb.has_value() && rb->epoch != ra->epoch);
	CHECK(cancelled == 1);
	CHECK(core.inflight() == 0);
	CHECK(!s.exists(ha) && !s.exists(ctxa) && !s.exists(leftover));   // A's graph reclaimed
	CHECK(s.exists(hb) && s.exists(ctxb));                             // B's request graph kept
	CHECK(rb->body.find("Sanctuary") != std::string::npos);
	// A completes late (reply and error): both discarded
	core.complete(ra->epoch, 200, R"({"mantella_replytype":"mantella_npc_talk","from":"A"})", "");
	core.complete(ra->epoch, 0, "", "timeout");
	CHECK(core.get_handle() == kNoReply);
	CHECK(core.stale_discarded() == 2);
	CHECK(core.begin_inflight(ra->epoch, [] {}) == 0);                 // stale job must not be sent
	// B's own reply is delivered
	core.complete(rb->epoch, 200, R"({"mantella_replytype":"mantella_npc_talk","from":"B"})", "");
	int g = core.get_handle();
	CHECK(g > 0 && g < kErrorOffset && s.get_string(g, "from", "") == "B");
	// a queued-but-unread reply of B is dropped by clearAllDictionaries (also a boundary)
	core.complete(rb->epoch, 200, R"({"x":1})", "");
	core.clear_all();
	CHECK(core.get_handle() == kNoReply);
	CHECK(s.size() == 0);
	// non-start requests do not bump the epoch
	int hc = s.create();
	s.set_string(hc, kRequestTypeKey, "mantella_continue_conversation");
	const auto e = core.epoch();
	CHECK(core.prepare(hc, 4999, "mantella", 0).has_value());
	CHECK(core.epoch() == e);
}
static void test_store_json_bridge() {
	DictStore s;
	auto v = json::parse(R"({
		"Mantella_ReplyType":"npc_talk", "N":{"Inner_Key":5,"Deep":{"X":true}},
		"Empty":[], "Ints":[1,2], "Mixed":[1,2.5], "Floats":[0.5], "Bools":[true,false],
		"Objs":[{"A":1},{"B":"two"}], "Strs":["a","b"], "Nul":null, "Het":[1,"a"], "Nest":[[1]],
		"Big": 9999999999 })");
	CHECK(v.has_value());
	int h = s.from_json(*v);
	CHECK(h > 0);
	CHECK(s.get_string(h, "mantella_replytype", "") == "npc_talk");
	int n = s.get_nested(h, "n", 0);
	CHECK(n > 0 && s.get_int(n, "inner_key", 0) == 5);          // nested keys lowercased
	CHECK(s.get_bool(s.get_nested(n, "deep", 0), "x", false));
	CHECK(*s.value_type_name(h, "empty") == std::string("stringArray"));  // upstream order: empty -> stringArray
	CHECK(s.get_int_array(h, "empty").empty());
	CHECK((s.get_int_array(h, "ints") == std::vector<std::int32_t>{ 1, 2 }));
	CHECK((s.get_float_array(h, "mixed") == std::vector<float>{ 1.0f, 2.5f }));  // upstream dropped this
	CHECK((s.get_bool_array(h, "bools") == std::vector<bool>{ true, false }));
	auto objs = s.get_nested_array(h, "objs");
	CHECK(objs.size() == 2 && s.get_int(objs[0], "a", 0) == 1 && s.get_string(objs[1], "b", "") == "two");
	CHECK(!s.has_key(h, "nul"));
	CHECK(!s.has_key(h, "het"));
	CHECK(!s.has_key(h, "nest"));
	CHECK(s.get_int(h, "big", 0) == INT32_MAX);                  // saturated, not wrapped
	CHECK(s.from_json(json::Value::array()) == 0);

	// A reply may not create more than capacity/2 dictionaries: it would otherwise evict its own
	// root mid-import and flush every handle the scripts hold.
	{
		DictStore small(10);
		int keep = small.create();
		small.set(keep, "k", std::int32_t{ 1 });
		std::string body = "{\"a\":[";
		for (int i = 0; i < 20; ++i) body += (i ? "," : "") + std::string("{\"x\":1}");
		body += "]}";
		auto flood = json::parse(body);
		CHECK(flood.has_value());
		CHECK(small.from_json(*flood) == 0);
		CHECK(small.get_int(keep, "k", 0) == 1);             // nothing evicted
		auto ok = json::parse(R"({"a":[{"x":1},{"x":2}],"b":{"c":{}}})");  // 5 dicts == 10/2
		CHECK(small.from_json(*ok) > 0);
	}

	// request direction incl. D4 cycle safety
	int r = s.create();
	s.set(r, "Name", std::string("Kalli"));
	s.set(r, "self", NestedRef{ r });                            // cycle
	int c = s.create();
	s.set(c, "f", 0.25f);
	s.set(r, "child", NestedRef{ c });
	s.set(r, "kids", NestedArray{ { c, 4242 } });
	auto out = s.to_json(r);
	CHECK(out.has_value());
	std::string body = json::dump(*out);
	auto back = json::parse(body);
	CHECK(back && back->type == json::Type::Object);
	std::map<std::string, const json::Value*> m;
	for (auto& [k, val] : back->obj) m[k] = &val;
	CHECK(m["name"]->s == "Kalli");
	CHECK(m["self"]->type == json::Type::Null);                  // cycle cut, no stack overflow
	CHECK(m["child"]->obj.size() == 1 && m["child"]->obj[0].second.f == 0.25);
	CHECK(m["kids"]->arr.size() == 2 && m["kids"]->arr[1].type == json::Type::Null);
}

static void test_store_threads() {
	// D2: concurrent reply import (HTTP thread) and Papyrus reads/writes must be safe.
	DictStore s(5000);
	std::atomic<bool> stop{ false };
	std::atomic<int> ops{ 0 };
	auto reply = json::parse(R"({"k":1,"arr":[1,2,3],"o":{"x":"y"}})");
	std::thread importer([&] { for (int i = 0; i < 3000; ++i) { s.from_json(*reply); ++ops; } stop = true; });
	std::thread papyrus([&] {
		while (!stop) {
			int h = s.create();
			s.set(h, "a", std::int32_t{ 1 });
			(void)s.get_int(h - 1, "k", 0);
			(void)s.to_json(h);
			++ops;
		}
	});
	std::thread clearer([&] { for (int i = 0; i < 50 && !stop; ++i) { s.clear_all(); std::this_thread::yield(); } });
	importer.join(); papyrus.join(); clearer.join();
	CHECK(ops.load() >= 3000);
	CHECK(s.size() <= 5000);
}

// ------------------------------------------------------------------------------- queue + request
static void test_queue_and_core() {
	ReplyQueue q(3);
	CHECK(q.pop() == kNoReply);
	q.push_reply(5); q.push_error(6);
	CHECK(q.pop() == 5);
	CHECK(q.pop() == 100006);
	CHECK(q.pop() == kNoReply);
	q.push_reply(1); q.push_reply(2); q.push_reply(3); q.push_reply(4);
	CHECK(q.dropped() == 1 && q.pop() == 2);

	DictStore s;
	ReplyQueue rq;
	HttpCore core(s, rq);
	int h = s.create();
	s.set_string(h, "mantella_request_type", "mantella_start_conversation");
	auto req = core.prepare(h, 4999, "/MantellA", 0);
	CHECK(req.has_value());
	CHECK(req->host == "localhost" && req->port == 4999 && req->path == "/mantella");
	CHECK(req->body == "{\"mantella_request_type\":\"mantella_start_conversation\"}");
	CHECK(req->connect_timeout_ms == 0);
	CHECK(core.get_handle() == kNoReply);

	auto expect_error = [&](const char* what) {
		int g = core.get_handle();
		CHECK(g > kErrorOffset);
		std::string msg = s.get_string(g - kErrorOffset, kErrorKey, "");
		CHECK(msg.find(what) != std::string::npos);
	};
	CHECK(!core.prepare(h, 0, "mantella", 0));            expect_error("invalid port");
	CHECK(!core.prepare(h, 70000, "mantella", 0));        expect_error("invalid port");
	CHECK(!core.prepare(h, 4999, "x@evil.example", 0));   expect_error("invalid route");
	CHECK(!core.prepare(h, 4999, "a/../b", 0));           expect_error("invalid route");
	CHECK(!core.prepare(h, 4999, "a?b=c", 0));            expect_error("invalid route");
	CHECK(!core.prepare(h, 4999, "", 0));                 expect_error("invalid route");
	CHECK(!core.prepare(999, 4999, "mantella", 0));       expect_error("unknown dictionary handle");

	core.complete(200, R"({"Mantella_ReplyType":"mantella_npc_talk"})", "");
	int got = core.get_handle();
	CHECK(got > 0 && got < kErrorOffset);
	CHECK(s.get_string(got, "mantella_replytype", "") == "mantella_npc_talk");
	core.complete(500, "boom", "");                       expect_error("HTTP status 500");
	core.complete(0, "", "Couldn't connect to server");   expect_error("connect");
	core.complete(200, "{not json", "");                  expect_error("invalid JSON reply");
	core.complete(200, "[1,2]", "");                      expect_error("not a JSON object");
	CHECK(core.get_handle() == kNoReply);
}

// --------------------------------------------------------------------------- contract vs scripts
struct PscNative { std::string ret, params; };

static std::string trim(std::string s) {
	auto b = s.find_first_not_of(" \t\r\n");
	auto e = s.find_last_not_of(" \t\r\n");
	return b == std::string::npos ? "" : s.substr(b, e - b + 1);
}

static std::string lc(std::string s) { return lower(std::move(s)); }

// Extracts active `<ret> function <name>(<params>) global native` declarations.
static std::map<std::string, PscNative> parse_psc(const std::string& path) {
	std::map<std::string, PscNative> out;
	std::ifstream f(path);
	std::string line;
	bool inBlock = false;
	while (std::getline(f, line)) {
		std::string t = trim(line);
		if (inBlock) { if (t.find("/;") != std::string::npos) inBlock = false; continue; }
		if (t.rfind(";/", 0) == 0) { if (t.find("/;", 2) == std::string::npos) inBlock = true; continue; }
		if (t.empty() || t[0] == ';') continue;
		std::string l = lc(t);
		auto fn = l.find("function ");
		auto nat = l.find(" native");
		if (fn == std::string::npos || nat == std::string::npos || l.find(" global") == std::string::npos) continue;
		std::string ret = trim(l.substr(0, fn));
		auto open = t.find('(', fn), close = t.find(')', fn);
		if (open == std::string::npos || close == std::string::npos) continue;
		std::string name = trim(t.substr(fn + 9, open - fn - 9));
		std::string params, cur;
		std::stringstream ps(t.substr(open + 1, close - open - 1));
		while (std::getline(ps, cur, ',')) {
			cur = trim(cur);
			if (cur.empty()) continue;
			std::string type = lc(cur.substr(0, cur.find_first_of(" \t")));
			params += (params.empty() ? "" : ",") + type;
		}
		out[lc(name)] = { ret, params };  // Papyrus identifiers are case-insensitive
	}
	return out;
}

static void test_contract(const char* officialPsc, const char* upstreamPsc) {
	std::set<std::string> names;
	for (const auto& c : kContract) CHECK(names.insert(std::string(c.name)).second);  // no duplicates
	if (!officialPsc) { std::fprintf(stderr, "skip: F4SEHTTP240_OFFICIAL_PSC not set\n"); return; }
	auto official = parse_psc(officialPsc);
	CHECK(official.size() == kContract.size());
	for (const auto& c : kContract) {
		auto it = official.find(lc(std::string(c.name)));
		CHECK(it != official.end());
		if (it == official.end()) { std::fprintf(stderr, "  missing in official PSC: %s\n", std::string(c.name).c_str()); continue; }
		if (it->second.ret != c.ret || it->second.params != c.params)
			std::fprintf(stderr, "  mismatch %s: psc '%s(%s)' table '%s(%s)'\n", std::string(c.name).c_str(),
				it->second.ret.c_str(), it->second.params.c_str(), std::string(c.ret).c_str(), std::string(c.params).c_str());
		CHECK(it->second.ret == c.ret);
		CHECK(it->second.params == c.params);
	}
	CHECK(!official.contains("valuetype"));            // commented out in the official script
	if (upstreamPsc) {
		auto upstream = parse_psc(upstreamPsc);
		CHECK(!upstream.contains("gethandle"));        // the official contract is a fork, not cdff839
		CHECK(upstream.contains("valuetype"));         // declared upstream but never bound there
		for (const auto& [n, sig] : upstream) {
			if (n == "valuetype") continue;
			auto it = official.find(n);
			CHECK(it != official.end() && it->second.ret == sig.ret && it->second.params == sig.params);
		}
	}
}

int main() {
	test_json();
	test_store_basics();
	test_store_handle_space();
	test_store_no_stale_alias();
	test_store_byte_budget();
	test_request_expansion_budget();
	test_conversation_epochs();
	test_concurrent_starts_atomic_epoch();
	test_store_json_bridge();
	test_store_threads();
	test_queue_and_core();
	test_contract(std::getenv("F4SEHTTP240_OFFICIAL_PSC"), std::getenv("F4SEHTTP240_UPSTREAM_PSC"));
	std::printf("test_http_core: %d checks, %d failures\n", checks, failures);
	return failures ? 1 : 0;
}
