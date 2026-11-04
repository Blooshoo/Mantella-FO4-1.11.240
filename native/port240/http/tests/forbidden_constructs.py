"""Comment-aware forbidden-construct gate for the F4SE_HTTP240 plugin sources.

The port must not reference upstream's pre-NG event IDs or use page-unprotect / input-injection
workarounds in EXECUTABLE code. A plain grep also matched the plugin's own explanatory comment
(Sol review A08), so C/C++ comments are stripped first (string/char literals are respected so a
"//" inside a string does not hide the rest of the line).

Usage: python3 -I forbidden_constructs.py --self-test FILE...   (exit 1 on any finding)
"""
import re
import sys

# REL::Module: pre-libxse CommonLib API, absent from libxse 7c8c6f8 (Sol review A01).
FORBIDDEN = re.compile(r"REL::ID\s*\(\s*(69733|445184)\s*\)|AddVectoredExceptionHandler|VirtualProtect|SendInput|keybd_event|REL::Module\b")


def strip_comments(src: str) -> str:
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            seg = src[i:n if j < 0 else j + 2]
            out.append("\n" * seg.count("\n"))  # keep line numbers
            i = n if j < 0 else j + 2
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def findings(src: str):
    for no, line in enumerate(strip_comments(src).splitlines(), 1):
        if FORBIDDEN.search(line):
            yield no, line.strip()


def self_test() -> None:
    assert not list(findings("// pre-NG REL::ID(69733)/REL::ID(445184) explained here\nint x;\n"))
    assert not list(findings("/* VirtualProtect\n SendInput */ int y;\n"))
    assert list(findings("auto f = REL::ID(69733);\n")) == [(1, "auto f = REL::ID(69733);")]
    assert list(findings("int a; /* c */ VirtualProtect(p, n, f, &o); // why\n"))
    assert list(findings('const char* s = "//"; SendInput(1, &in, sizeof in);\n'))
    assert list(findings("/* one */\n\nREL::ID( 445184 );\n")) == [(3, "REL::ID( 445184 );")]
    assert list(findings("auto b = REL::Module::get().base();\n"))
    assert not list(findings("auto b = REX::FModule::GetExecutingModule();\n"))


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--self-test":
        self_test()
        argv = argv[1:]
    bad = 0
    for path in argv:
        with open(path, encoding="utf-8") as f:
            for no, line in findings(f.read()):
                print(f"{path}:{no}: forbidden construct: {line}", file=sys.stderr)
                bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
