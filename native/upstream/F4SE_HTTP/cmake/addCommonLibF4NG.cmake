macro(find_commonlib_path)
	if (CommonLibName AND NOT ${CommonLibName} STREQUAL "")
		# Check extern
		find_path(CommonLibPath
		include/REL/Relocation.h
		PATHS ${ROOT_DIR}/extern/${CommonLibName}/${CommonLibName}/)
		if (${CommonLibPath} STREQUAL "CommonLibPath-NOTFOUND")
			#Check path
			set_from_environment(${CommonLibName}Path)
			set(CommonLibPath ${${CommonLibName}Path})
		endif()
	endif()
endmacro()
set(CommonLibName "CommonLibF4")
set_root_directory()

find_commonlib_path()
message(
	STATUS
	"Building ${PROJECT_NAME} ${PROJECT_VERSION} for ${FalloutVersion} with ${CommonLibName} at ${CommonLibPath}."
)

if (DEFINED CommonLibPath AND NOT ${CommonLibPath} STREQUAL "" AND IS_DIRECTORY ${CommonLibPath})
	add_subdirectory(${CommonLibPath} ${CommonLibName})
else ()
	message(
		FATAL_ERROR
		"Variable ${CommonLibName}Path is not set or in external/."
	)
endif()

find_package(Boost
	MODULE
	REQUIRED
	COMPONENTS
		nowide
		#stacktrace_windbg
)
#IF (Boost_FOUND)
#    INCLUDE_DIRECTORIES(${Boost_INCLUDE_DIR})
#    ADD_DEFINITIONS( "-DHAS_BOOST" )
#ENDIF()
find_package(binary_io REQUIRED CONFIG)
find_package(fmt REQUIRED CONFIG)
find_package(frozen REQUIRED CONFIG)
find_package(spdlog REQUIRED CONFIG)
find_package(mmio REQUIRED CONFIG)
find_package(xbyak REQUIRED CONFIG)
list(APPEND CMAKE_MODULE_PATH "${PROJECT_SOURCE_DIR}/cmake")

target_link_libraries(
	"${PROJECT_NAME}"
	PRIVATE
		Boost::headers
		Boost::nowide
		#Boost::stacktrace_windbg
		Dbghelp.lib
		binary_io::binary_io
		CommonLibF4::CommonLibF4
		fmt::fmt
		frozen::frozen
		mmio::mmio
		spdlog::spdlog
		xbyak::xbyak
)

#target_link_libraries(
#	"${PROJECT_NAME}"
#	PRIVATE
#		Dbghelp.lib
#		CommonLibF4::CommonLibF4
#		mmio::mmio
#		spdlog::spdlog
#		xbyak::xbyak
#)

#target_compile_definitions(
#	CommonLibF4
#	PUBLIC
#		F4SE_SUPPORT_XBYAK
#)
