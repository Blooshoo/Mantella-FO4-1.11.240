"""Static lint of F4SE plugin version declarations (no build needed).

Official F4SE 0.7.9 PluginManager::CheckPluginCompatibility treats a plugin whose version data sets
kAddressIndependence_AddressLibrary_1_11_137 AND kStructureIndependence_1_11_137Layout as version
independent: it then loads on EVERY 1.11.x runtime and ignores compatibleVersions. With CommonLib's
PluginVersionData those bits are set by UsesAddressLibrary(true) and IsLayoutDependent(true).
A port that is only verified on 1.11.240 must therefore not set both, must not use RUNTIME_LATEST
(which follows whatever CommonLib revision is used), and must list 1.11.240 explicitly.
"""
import re
import sys

def block(src: str) -> str:
    m = re.search(r"F4SE(?:_EXPORT\s+constinit\s+auto\s+F4SEPlugin_Version|_PLUGIN_VERSION)\s*=.*?\}\(\);", src, re.S)
    return m.group(0) if m else ""

def main(paths):
    bad = 0
    for p in paths:
        b = block(open(p, encoding="utf-8", errors="replace").read())
        problems = []
        if not b:
            problems.append("no version data block found")
        else:
            addr = re.search(r"UsesAddressLibrary\(\s*true\s*\)", b)
            layout = re.search(r"IsLayoutDependent\(\s*true\s*\)", b)
            if addr and layout:
                problems.append("claims AddressLibrary_1_11_137 + 1_11_137Layout => F4SE loads it on every 1.11.x")
            if re.search(r"RUNTIME_LATEST(?!_VR)", b):
                problems.append("uses RUNTIME_LATEST (tracks the CommonLib revision, not a verified runtime)")
            if not re.search(r"(1,\s*11,\s*240|kRuntime240|RUNTIME_1_11_240)", b):
                problems.append("does not list 1.11.240 explicitly")
        print(("FAIL " if problems else "OK   ") + p + ("" if not problems else ": " + "; ".join(problems)))
        bad += bool(problems)
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
