// F4SE_HTTP for Fallout 4 1.11.240 (F4SE 0.7.9) on libxse/commonlibf4 7c8c6f8.
//
// Port of Leidtier/F4SE_HTTP cdff839 (MIT, see upstream/F4SE_HTTP/LICENSE) to the OFFICIAL Mantella
// FO4 Papyrus contract (F4SE_HTTP.psc with GetHandle polling). Original port code: MIT.
// STATUS: static source only. Not compiled (no MSVC/clang-cl + Windows SDK on BlooDesk), not
// loaded in a game, not runtime-accepted.
//
// What changed vs upstream and why:
//   * No Papyrus event dispatch at all. Upstream built engine function-argument objects through
//     pre-NG REL::ID(69733)/REL::ID(445184) and leaked a heap ArrayWrapper per reply; the official
//     scripts poll GetHandle() instead, so replies are only queued. This removes every
//     BSTThreadScrapFunction / DispatchMethodCall ABI dependency.
//   * Dictionary store / JSON / request validation moved into the tested header-only core
//     (port240/http/include/f4sehttp240), which fixes upstream defects D1-D5 (dict_store.h).
//   * Transport: WinHTTP on a small bounded worker pool (no cpr/curl/vcpkg dependency); redirects are
//     never followed. Natives do no network I/O; building a request body is bounded by an aggregate
//     node/byte budget (dict_store.h D6). HTTP completion only touches the mutex-guarded store and
//     queue, never the VM. Replies are tagged with a conversation epoch (http_core.h) so a late
//     reply cannot answer a later conversation; stale in-flight requests are cancelled cooperatively.
//   * Load gate: refuses to load (returns false before registering anything) unless the runtime is
//     exactly 1.11.240, not the editor, and every required engine ID passes ids_check.h: IDs with
//     an F4SE 0.7.9 second source must agree with it; the two stack-frame helpers have no second
//     source and get section/alignment plausibility checks only (ids240.h). A refused plugin leaves natives unbound: Papyrus logs
//     "unbound native function" errors, Mantella cannot talk, and nothing engine-side is touched.
//   * Version data declares ONLY 1.11.240 and claims no address/structure independence, because
//     official F4SE treats AddressLibrary_1_11_137 + 1_11_137Layout as "loads on every 1.11.x"
//     (PluginManager::CheckPluginCompatibility) — upstream and the libxse plugin template both set them.
//   * The 0x97 "data ready" key press of the official DLL is NOT synthesized (no input injection);
//     the scripts' poll timer picks replies up within one poll period.

#include <F4SE/F4SE.h>
#include <RE/Fallout.h>

#include "f4sehttp240/http_core.h"
#include "f4sehttp240/ids_check.h"
#include "transport.h"  // Win32/WinHTTP live in transport_winhttp.cpp, never in this TU

#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <span>
#include <thread>

namespace {

using namespace f4sehttp240;

constexpr REL::Version kRuntime240{ 1, 11, 240, 0 };
constexpr std::size_t kMaxPendingRequests = 32;
// A cancelled request that is already blocked inside WinHTTP keeps its worker until the step
// returns or times out (cooperative cancellation, transport.h), so keep spare workers.
constexpr unsigned kWorkers = 4;

// Process-lifetime state, intentionally leaked: the detached workers below may still be blocked
// on the condition variable (or inside WinHTTP) when the CRT runs static destructors at exit;
// destroying a mutex/condition_variable/store under them would be undefined behaviour.
DictStore& g_store = *new DictStore();
ReplyQueue& g_queue = *new ReplyQueue();
HttpCore& g_core = *new HttpCore(g_store, g_queue);

class Workers {
public:
	// Detached on purpose: a joinable/jthread member would block in its destructor at game exit,
	// because the workers sleep on the condition variable forever. Idempotent.
	void start() {
		std::lock_guard lk(mu_);
		if (started_) return;
		started_ = true;
		for (unsigned i = 0; i < kWorkers; ++i) std::thread([this] { run(); }).detach();
	}
	bool enqueue(Request r) {
		{
			std::lock_guard lk(mu_);
			if (jobs_.size() >= kMaxPendingRequests) return false;
			jobs_.push_back(std::move(r));
		}
		cv_.notify_one();
		return true;
	}
private:
	void run() {
		for (;;) {
			Request r;
			{
				std::unique_lock lk(mu_);
				cv_.wait(lk, [this] { return !jobs_.empty(); });
				r = std::move(jobs_.front());
				jobs_.pop_front();
			}
			try {
				// Register before sending; a job whose conversation already ended is dropped unsent.
				auto token = std::make_shared<win::CancelToken>();
				const auto id = g_core.begin_inflight(r.epoch, [token] { token->cancel(); });
				if (!id) continue;
				struct End { std::uint64_t id; ~End() { g_core.end_inflight(id); } } end{ id };  // also on exceptions
				win::HttpResult res = win::post(r, json::Limits{}.max_bytes, *token);
				g_core.complete(r.epoch, res.status, res.body, res.error);  // stale epochs are discarded
			} catch (...) {
				g_core.complete(r.epoch, 0, {}, "internal error while sending request");
			}
		}
	}
	std::mutex mu_;
	std::condition_variable cv_;
	std::deque<Request> jobs_;
	bool started_ = false;
};
Workers& g_workers = *new Workers();

// ---------------------------------------------------------------------------------- natives
// Signatures follow libxse BindNativeMethod: (std::monostate, args...) for global natives.
// Parameter/return marshalling and ownership: see contract.h.
using Static = std::monostate;

void sendLocalhostHttpRequest(Static, std::int32_t handle, std::int32_t port, std::string route, std::int32_t timeout) {
	auto req = g_core.prepare(handle, port, std::move(route), timeout);
	if (!req) return;
	const auto epoch = req->epoch;
	if (!g_workers.enqueue(std::move(*req))) g_core.complete(epoch, 0, {}, "too many pending requests");
}
std::int32_t createDictionary(Static) { return g_store.create(); }
// Lifecycle boundary: also drops queued replies and cancels in-flight requests (http_core.h epochs).
void clearAllDictionaries(Static) { g_core.clear_all(); }
std::int32_t GetHandle(Static) { return g_core.get_handle(); }

std::string getString(Static, std::int32_t o, std::string k, std::string d) { return g_store.get_string(o, std::move(k), std::move(d)); }
std::int32_t getInt(Static, std::int32_t o, std::string k, std::int32_t d) { return g_store.get_int(o, std::move(k), d); }
float getFloat(Static, std::int32_t o, std::string k, float d) { return g_store.get_float(o, std::move(k), d); }
bool getBool(Static, std::int32_t o, std::string k, bool d) { return g_store.get_bool(o, std::move(k), d); }
std::int32_t getNestedDictionary(Static, std::int32_t o, std::string k, std::int32_t d) { return g_store.get_nested(o, std::move(k), d); }
std::vector<std::int32_t> getIntArray(Static, std::int32_t o, std::string k) { return g_store.get_int_array(o, std::move(k)); }
std::vector<float> getFloatArray(Static, std::int32_t o, std::string k) { return g_store.get_float_array(o, std::move(k)); }
std::vector<std::string> getStringArray(Static, std::int32_t o, std::string k) { return g_store.get_string_array(o, std::move(k)); }
std::vector<bool> getBoolArray(Static, std::int32_t o, std::string k) { return g_store.get_bool_array(o, std::move(k)); }
std::vector<std::int32_t> getNestedDictionariesArray(Static, std::int32_t o, std::string k) { return g_store.get_nested_array(o, std::move(k)); }

bool setString(Static, std::int32_t o, std::string k, std::string v) { return g_store.set_string(o, std::move(k), std::move(v)); }
void setInt(Static, std::int32_t o, std::string k, std::int32_t v) { g_store.set(o, std::move(k), v); }
void setFloat(Static, std::int32_t o, std::string k, float v) { g_store.set(o, std::move(k), v); }
void setBool(Static, std::int32_t o, std::string k, bool v) { g_store.set(o, std::move(k), v); }
void setNestedDictionary(Static, std::int32_t o, std::string k, std::int32_t v) { g_store.set(o, std::move(k), NestedRef{ v }); }
void setIntArray(Static, std::int32_t o, std::string k, std::vector<std::int32_t> v) { g_store.set(o, std::move(k), std::move(v)); }
void setFloatArray(Static, std::int32_t o, std::string k, std::vector<float> v) { g_store.set(o, std::move(k), std::move(v)); }
bool setStringArray(Static, std::int32_t o, std::string k, std::vector<std::string> v) { return g_store.set_string_array(o, std::move(k), v); }
void setBoolArray(Static, std::int32_t o, std::string k, std::vector<bool> v) { g_store.set(o, std::move(k), std::move(v)); }
void setNestedDictionariesArray(Static, std::int32_t o, std::string k, std::vector<std::int32_t> v) { g_store.set(o, std::move(k), NestedArray{ std::move(v) }); }
bool hasKey(Static, std::int32_t o, std::string k) { return g_store.has_key(o, std::move(k)); }

bool Bind(RE::BSScript::IVirtualMachine* vm) {
	constexpr std::string_view cls = "F4SE_HTTP";
	// Names/types must match contract.h (checked against the official PSC by test_http_core).
	vm->BindNativeMethod(cls, "sendLocalhostHttpRequest", sendLocalhostHttpRequest);
	vm->BindNativeMethod(cls, "createDictionary", createDictionary);
	vm->BindNativeMethod(cls, "clearAllDictionaries", clearAllDictionaries);
	vm->BindNativeMethod(cls, "GetHandle", GetHandle);
	vm->BindNativeMethod(cls, "getString", getString);
	vm->BindNativeMethod(cls, "getInt", getInt);
	vm->BindNativeMethod(cls, "getFloat", getFloat);
	vm->BindNativeMethod(cls, "getBool", getBool);
	vm->BindNativeMethod(cls, "getNestedDictionary", getNestedDictionary);
	vm->BindNativeMethod(cls, "getIntArray", getIntArray);
	vm->BindNativeMethod(cls, "getFloatArray", getFloatArray);
	vm->BindNativeMethod(cls, "getStringArray", getStringArray);
	vm->BindNativeMethod(cls, "getBoolArray", getBoolArray);
	vm->BindNativeMethod(cls, "getNestedDictionariesArray", getNestedDictionariesArray);
	vm->BindNativeMethod(cls, "setString", setString);
	vm->BindNativeMethod(cls, "setInt", setInt);
	vm->BindNativeMethod(cls, "setFloat", setFloat);
	vm->BindNativeMethod(cls, "setBool", setBool);
	vm->BindNativeMethod(cls, "setNestedDictionary", setNestedDictionary);
	vm->BindNativeMethod(cls, "setIntArray", setIntArray);
	vm->BindNativeMethod(cls, "setFloatArray", setFloatArray);
	vm->BindNativeMethod(cls, "setStringArray", setStringArray);
	vm->BindNativeMethod(cls, "setBoolArray", setBoolArray);
	vm->BindNativeMethod(cls, "setNestedDictionariesArray", setNestedDictionariesArray);
	vm->BindNativeMethod(cls, "hasKey", hasKey);
	return true;
}

// ---------------------------------------------------------------------------------- load gate
bool gate(const F4SE::LoadInterface* f4se) {
	if (f4se->IsEditor()) { REX::CRITICAL("F4SE_HTTP240: editor not supported"); return false; }
	if (f4se->RuntimeVersion() != kRuntime240) { REX::CRITICAL("F4SE_HTTP240: runtime is not 1.11.240, refusing to load"); return false; }

	// Same file libxse's REL::ID uses; read independently so the check does not trust libxse's parser.
	const std::string addrlib = win::exe_directory() + "Data\\F4SE\\Plugins\\version-1-11-240-0.bin";
	const auto bytes = win::read_file(addrlib, 64u << 20);
	auto lib = mantella240::AddressLibrary::parse(bytes);
	if (!lib) { REX::CRITICAL("F4SE_HTTP240: cannot read {}", addrlib); return false; }

	const auto base = win::module_base();
	const auto size = win::image_size(base);
	if (!size) { REX::CRITICAL("F4SE_HTTP240: bad module headers"); return false; }
	std::span<const std::uint8_t> image(reinterpret_cast<const std::uint8_t*>(base), size);
	auto pe = mantella240::PeImage::parse(image, false);
	if (!pe) { REX::CRITICAL("F4SE_HTTP240: cannot parse loaded image"); return false; }

	auto rep = evaluate_ids(*lib, *pe);
	for (const auto& r : rep.results) REX::INFO("F4SE_HTTP240 id {} {}: {}", r.spec->id, r.spec->name, r.detail);
	if (!rep.all_required_ok()) { REX::CRITICAL("F4SE_HTTP240: engine ID verification failed, refusing to load"); return false; }
	return true;
}

}  // namespace

F4SE_PLUGIN_LOAD(const F4SE::LoadInterface* a_f4se) {
	F4SE::Init(a_f4se, { .log = true, .logName = "F4SE_HTTP240", .hook = false });
	if (!gate(a_f4se)) return false;  // nothing registered (F4SE: do not register anything when returning false)
	const auto papyrus = F4SE::GetPapyrusInterface();
	if (!papyrus) { REX::CRITICAL("F4SE_HTTP240: no Papyrus interface"); return false; }
	if (!papyrus->Register(Bind)) { REX::CRITICAL("F4SE_HTTP240: Papyrus registration failed"); return false; }
	g_workers.start();  // after registration; requests queued before this are picked up on start
	REX::INFO("F4SE_HTTP240 loaded (GetHandle polling; 0x97 key hint not synthesized)");
	return true;
}

F4SE_PLUGIN_VERSION = []() noexcept {
	F4SE::PluginVersionData v{};
	v.PluginName("F4SE_HTTP");  // same name the official Mantella package uses, so only one can load
	v.PluginVersion({ 1, 240, 0, 0 });
	v.AuthorName("Leidtier (upstream), kalli/port-240");
	v.UsesSigScanning(false);
	v.UsesAddressLibrary(false);  // deliberately: claiming it makes F4SE load us on every 1.11.x
	v.HasNoStructUse(false);
	v.IsLayoutDependent(false);   // deliberately: same reason; compatibility = the explicit list below
	v.CompatibleVersions({ kRuntime240 });
	return v;
}();
