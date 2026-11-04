-- Isolated build recipe for F4SE_HTTP240.dll (NOT executed on BlooDesk: no MSVC/clang-cl + Windows SDK).
--
-- Requirements (Windows build host, or a cross host with clang-cl + lld-link + xwin-provided SDK/CRT):
--   * XMake >= 3.0.0, a C++23 compiler (MSVC 19.38+ or clang-cl 17+), Windows 10 SDK (winhttp.lib)
--   * libxse/commonlibf4 at 7c8c6f8a349ac85abf6fc8a7e5a71732d9339954 with submodules, checked out at
--     ./extern/commonlibf4 and patched with ../patches/libxse-7c8c6f8-null-array.patch
--
-- Safety (mandatory): run in a scratch directory; NEVER set XSE_FO4_MODS_PATH or XSE_FO4_GAME_PATH
-- (libxse install rules copy into the game/MO2 if they are set). This file does not use the
-- commonlibf4.plugin rule at all: that rule generates version data with RUNTIME_LATEST and
-- address/layout independence, which official F4SE would load on every 1.11.x runtime.
-- The produced DLL is an artifact only; staging into MO2 is a separate, owner-authorised step.
set_xmakever("3.0.0")
set_project("F4SE_HTTP240")
set_languages("c++23")
set_warnings("allextra")
set_encodings("utf-8")
add_rules("mode.releasedbg")

if os.getenv("XSE_FO4_MODS_PATH") or os.getenv("XSE_FO4_GAME_PATH") then
    raise("refusing to configure: unset XSE_FO4_MODS_PATH / XSE_FO4_GAME_PATH (would install into the game/MO2)")
end

includes("extern/commonlibf4")

target("F4SE_HTTP240", function()
    set_kind("shared")
    add_deps("commonlibf4")
    add_files("F4SE_HTTP240.cpp", "transport_winhttp.cpp")
    add_includedirs("../include", "../../include")
    add_syslinks("winhttp")
    set_basename("F4SE_HTTP")
    -- no installfiles / no after_build copy: artifacts stay in build/
end)
