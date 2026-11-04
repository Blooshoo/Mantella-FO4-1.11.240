// Minimal, bounded JSON value / parser / serializer for the F4SE_HTTP 1.11.240 port.
//
// Original work for this port (MIT, same as the F4SE_HTTP plugin it is linked into).
// Replaces the nlohmann::json dependency of upstream F4SE_HTTP cdff839 in the engine-independent
// core so the core builds and is tested on any C++20 compiler. Design constraints:
//   * hostile/huge input from the HTTP peer must never crash the game: byte, depth and node
//     limits are enforced while parsing (upstream recursed without a depth limit);
//   * the number model matches what upstream relied on from nlohmann: a literal without
//     fraction/exponent is an "integer", everything else is a "float";
//   * dump() never throws; invalid UTF-8 in strings is replaced with U+FFFD (nlohmann threw,
//     and upstream's catch(...) then silently dropped the whole request).
#pragma once

#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace f4sehttp240::json {

enum class Type : std::uint8_t { Null, Bool, Int, Float, String, Array, Object };

struct Value {
	Type type = Type::Null;
	bool b = false;
	std::int64_t i = 0;
	double f = 0.0;
	std::string s;
	std::vector<Value> arr;
	std::vector<std::pair<std::string, Value>> obj;  // insertion order; duplicate keys kept (last wins on import)

	static Value null() { return {}; }
	static Value boolean(bool v) { Value x; x.type = Type::Bool; x.b = v; return x; }
	static Value integer(std::int64_t v) { Value x; x.type = Type::Int; x.i = v; return x; }
	static Value number(double v) { Value x; x.type = Type::Float; x.f = v; return x; }
	static Value string(std::string v) { Value x; x.type = Type::String; x.s = std::move(v); return x; }
	static Value array() { Value x; x.type = Type::Array; return x; }
	static Value object() { Value x; x.type = Type::Object; return x; }
};

struct Limits {
	std::size_t max_bytes = 8u << 20;  // 8 MiB reply body
	std::size_t max_depth = 64;        // nesting depth
	std::size_t max_nodes = 200000;    // total values
	std::size_t max_string = 1u << 20; // single string, bytes
};

inline bool valid_utf8(std::string_view s) {
	std::size_t i = 0, n = s.size();
	while (i < n) {
		unsigned char c = static_cast<unsigned char>(s[i]);
		if (c < 0x80) { ++i; continue; }
		std::size_t len;
		std::uint32_t cp = 0;
		if ((c & 0xE0) == 0xC0) { len = 2; cp = c & 0x1F; }
		else if ((c & 0xF0) == 0xE0) { len = 3; cp = c & 0x0F; }
		else if ((c & 0xF8) == 0xF0) { len = 4; cp = c & 0x07; }
		else return false;
		if (i + len > n) return false;
		for (std::size_t k = 1; k < len; ++k) {
			unsigned char cc = static_cast<unsigned char>(s[i + k]);
			if ((cc & 0xC0) != 0x80) return false;
			cp = (cp << 6) | (cc & 0x3F);
		}
		if ((len == 2 && cp < 0x80) || (len == 3 && cp < 0x800) || (len == 4 && cp < 0x10000)) return false;  // overlong
		if (cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return false;
		i += len;
	}
	return true;
}

namespace detail {

inline void append_utf8(std::string& out, std::uint32_t cp) {
	if (cp < 0x80) out += static_cast<char>(cp);
	else if (cp < 0x800) { out += static_cast<char>(0xC0 | (cp >> 6)); out += static_cast<char>(0x80 | (cp & 0x3F)); }
	else if (cp < 0x10000) { out += static_cast<char>(0xE0 | (cp >> 12)); out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F)); out += static_cast<char>(0x80 | (cp & 0x3F)); }
	else { out += static_cast<char>(0xF0 | (cp >> 18)); out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F)); out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F)); out += static_cast<char>(0x80 | (cp & 0x3F)); }
}

class Parser {
public:
	Parser(std::string_view in, const Limits& lim) : s_(in), lim_(lim) {}

	std::optional<Value> run(std::string* err) {
		if (s_.size() > lim_.max_bytes) { fail("input exceeds byte limit"); return finish(err); }
		ws();
		Value v;
		if (!value(v, 0)) return finish(err);
		ws();
		if (p_ != s_.size()) { fail("trailing characters"); return finish(err); }
		return v;
	}

private:
	std::optional<Value> finish(std::string* err) { if (err) *err = err_ + " at offset " + std::to_string(p_); return std::nullopt; }
	bool fail(const char* m) { if (err_.empty()) err_ = m; return false; }
	void ws() { while (p_ < s_.size() && (s_[p_] == ' ' || s_[p_] == '\t' || s_[p_] == '\n' || s_[p_] == '\r')) ++p_; }
	bool lit(std::string_view w) { if (s_.substr(p_, w.size()) == w) { p_ += w.size(); return true; } return fail("invalid literal"); }

	bool value(Value& out, std::size_t depth) {
		if (++nodes_ > lim_.max_nodes) return fail("node limit exceeded");
		if (p_ >= s_.size()) return fail("unexpected end");
		char c = s_[p_];
		switch (c) {
			case 'n': out = Value::null(); return lit("null");
			case 't': out = Value::boolean(true); return lit("true");
			case 'f': out = Value::boolean(false); return lit("false");
			case '"': out = Value::string({}); return str(out.s);
			case '[': return arr(out, depth);
			case '{': return obj(out, depth);
			default: return num(out);
		}
	}

	bool arr(Value& out, std::size_t depth) {
		if (depth + 1 > lim_.max_depth) return fail("depth limit exceeded");
		out = Value::array();
		++p_;
		ws();
		if (p_ < s_.size() && s_[p_] == ']') { ++p_; return true; }
		for (;;) {
			Value v;
			ws();
			if (!value(v, depth + 1)) return false;
			out.arr.push_back(std::move(v));
			ws();
			if (p_ >= s_.size()) return fail("unterminated array");
			if (s_[p_] == ',') { ++p_; continue; }
			if (s_[p_] == ']') { ++p_; return true; }
			return fail("expected , or ]");
		}
	}

	bool obj(Value& out, std::size_t depth) {
		if (depth + 1 > lim_.max_depth) return fail("depth limit exceeded");
		out = Value::object();
		++p_;
		ws();
		if (p_ < s_.size() && s_[p_] == '}') { ++p_; return true; }
		for (;;) {
			ws();
			if (p_ >= s_.size() || s_[p_] != '"') return fail("expected key");
			std::string key;
			if (!str(key)) return false;
			ws();
			if (p_ >= s_.size() || s_[p_] != ':') return fail("expected :");
			++p_;
			ws();
			Value v;
			if (!value(v, depth + 1)) return false;
			out.obj.emplace_back(std::move(key), std::move(v));
			ws();
			if (p_ >= s_.size()) return fail("unterminated object");
			if (s_[p_] == ',') { ++p_; continue; }
			if (s_[p_] == '}') { ++p_; return true; }
			return fail("expected , or }");
		}
	}

	bool hex4(std::uint32_t& cp) {
		if (p_ + 4 > s_.size()) return fail("short \\u escape");
		cp = 0;
		for (int k = 0; k < 4; ++k) {
			char h = s_[p_++];
			cp <<= 4;
			if (h >= '0' && h <= '9') cp |= static_cast<std::uint32_t>(h - '0');
			else if (h >= 'a' && h <= 'f') cp |= static_cast<std::uint32_t>(h - 'a' + 10);
			else if (h >= 'A' && h <= 'F') cp |= static_cast<std::uint32_t>(h - 'A' + 10);
			else return fail("bad hex digit");
		}
		return true;
	}

	bool str(std::string& out) {
		++p_;  // opening quote
		std::size_t start = p_;
		for (;;) {
			if (p_ >= s_.size()) return fail("unterminated string");
			if (out.size() > lim_.max_string) return fail("string limit exceeded");
			unsigned char c = static_cast<unsigned char>(s_[p_]);
			if (c == '"') { ++p_; break; }
			if (c < 0x20) return fail("control character in string");
			if (c != '\\') { out += static_cast<char>(c); ++p_; continue; }
			++p_;
			if (p_ >= s_.size()) return fail("bad escape");
			char e = s_[p_++];
			switch (e) {
				case '"': out += '"'; break;
				case '\\': out += '\\'; break;
				case '/': out += '/'; break;
				case 'b': out += '\b'; break;
				case 'f': out += '\f'; break;
				case 'n': out += '\n'; break;
				case 'r': out += '\r'; break;
				case 't': out += '\t'; break;
				case 'u': {
					std::uint32_t cp = 0;
					if (!hex4(cp)) return false;
					if (cp >= 0xD800 && cp <= 0xDBFF) {
						std::uint32_t lo = 0;
						if (p_ + 2 > s_.size() || s_[p_] != '\\' || s_[p_ + 1] != 'u') return fail("lone high surrogate");
						p_ += 2;
						if (!hex4(lo) || lo < 0xDC00 || lo > 0xDFFF) return fail("bad low surrogate");
						cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
					} else if (cp >= 0xDC00 && cp <= 0xDFFF) {
						return fail("lone low surrogate");
					}
					append_utf8(out, cp);
					break;
				}
				default: return fail("bad escape");
			}
		}
		(void)start;
		if (!valid_utf8(out)) return fail("invalid UTF-8 in string");
		return true;
	}

	bool num(Value& out) {
		std::size_t b = p_;
		bool is_float = false;
		if (p_ < s_.size() && s_[p_] == '-') ++p_;
		if (p_ >= s_.size()) return fail("bad number");
		if (s_[p_] == '0') ++p_;
		else if (s_[p_] >= '1' && s_[p_] <= '9') { while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') ++p_; }
		else return fail("unexpected character");
		if (p_ < s_.size() && s_[p_] == '.') {
			is_float = true; ++p_;
			if (p_ >= s_.size() || s_[p_] < '0' || s_[p_] > '9') return fail("bad fraction");
			while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') ++p_;
		}
		if (p_ < s_.size() && (s_[p_] == 'e' || s_[p_] == 'E')) {
			is_float = true; ++p_;
			if (p_ < s_.size() && (s_[p_] == '+' || s_[p_] == '-')) ++p_;
			if (p_ >= s_.size() || s_[p_] < '0' || s_[p_] > '9') return fail("bad exponent");
			while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') ++p_;
		}
		// std::from_chars: locale independent (strtod follows the process locale, which the game or
		// another plugin may change to one with a ',' decimal separator).
		const char* first = s_.data() + b;
		const char* last = s_.data() + p_;
		if (!is_float) {
			std::int64_t v = 0;
			auto [ptr, ec] = std::from_chars(first, last, v);
			if (ec == std::errc() && ptr == last) { out = Value::integer(v); return true; }
			// out of int64 range: keep as float like nlohmann's unsigned/float fallback
		}
		double d = 0.0;
		auto [ptr, ec] = std::from_chars(first, last, d, std::chars_format::general);
		if (ec != std::errc() || ptr != last) return fail("number out of range");  // nlohmann: error 406
		out = Value::number(d);
		return true;
	}

	std::string_view s_;
	const Limits& lim_;
	std::size_t p_ = 0;
	std::size_t nodes_ = 0;
	std::string err_;
};

inline void dump_string(std::string& out, std::string_view s) {
	out += '"';
	std::size_t i = 0;
	while (i < s.size()) {
		unsigned char c = static_cast<unsigned char>(s[i]);
		if (c >= 0x80) {
			// copy one valid UTF-8 sequence or emit U+FFFD for an invalid byte
			std::size_t len = (c & 0xE0) == 0xC0 ? 2 : (c & 0xF0) == 0xE0 ? 3 : (c & 0xF8) == 0xF0 ? 4 : 0;
			if (len && i + len <= s.size() && valid_utf8(s.substr(i, len))) { out.append(s.substr(i, len)); i += len; }
			else { out += "\xEF\xBF\xBD"; ++i; }
			continue;
		}
		switch (c) {
			case '"': out += "\\\""; break;
			case '\\': out += "\\\\"; break;
			case '\b': out += "\\b"; break;
			case '\f': out += "\\f"; break;
			case '\n': out += "\\n"; break;
			case '\r': out += "\\r"; break;
			case '\t': out += "\\t"; break;
			default:
				if (c < 0x20) { char buf[16]; std::snprintf(buf, sizeof buf, "\\u%04x", static_cast<unsigned>(c)); out += buf; }
				else out += static_cast<char>(c);
		}
		++i;
	}
	out += '"';
}

inline void dump(std::string& out, const Value& v) {
	switch (v.type) {
		case Type::Null: out += "null"; break;
		case Type::Bool: out += v.b ? "true" : "false"; break;
		case Type::Int: out += std::to_string(v.i); break;
		case Type::Float: {
			if (!std::isfinite(v.f)) { out += "null"; break; }  // nlohmann behaviour for NaN/Inf
			char buf[64];
			auto res = std::to_chars(buf, buf + sizeof buf, v.f);  // shortest round-trip, locale independent
			std::string t(buf, res.ec == std::errc() ? res.ptr : buf);
			if (t.empty()) t = "0";
			if (t.find_first_of(".eE") == std::string::npos) t += ".0";  // keep float-ness for the peer
			out += t;
			break;
		}
		case Type::String: dump_string(out, v.s); break;
		case Type::Array:
			out += '[';
			for (std::size_t k = 0; k < v.arr.size(); ++k) { if (k) out += ','; dump(out, v.arr[k]); }
			out += ']';
			break;
		case Type::Object:
			out += '{';
			for (std::size_t k = 0; k < v.obj.size(); ++k) { if (k) out += ','; dump_string(out, v.obj[k].first); out += ':'; dump(out, v.obj[k].second); }
			out += '}';
			break;
	}
}

}  // namespace detail

inline std::optional<Value> parse(std::string_view in, const Limits& lim = {}, std::string* err = nullptr) {
	return detail::Parser(in, lim).run(err);
}

inline std::string dump(const Value& v) {
	std::string out;
	detail::dump(out, v);
	return out;
}

}  // namespace f4sehttp240::json
