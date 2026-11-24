cmake_minimum_required(VERSION 3.22)
project(comic_sol_raster LANGUAGES C)

include(ExternalProject)
include("${CMAKE_CURRENT_LIST_DIR}/../../../requirements/locks/native-raster.cmake")
if(POLICY CMP0135)
    cmake_policy(SET CMP0135 NEW)
endif()

# Match the native libraries in the reference Pillow 13 wheels.
function(codec name url hash archive)
    if(COMIC_SOL_CODEC_ARCHIVES)
        file(TO_CMAKE_PATH "${COMIC_SOL_CODEC_ARCHIVES}/${archive}" url)
    endif()
    ExternalProject_Add(${name}
        URL "${url}"
        URL_HASH "SHA256=${hash}"
        DOWNLOAD_NAME "${archive}"
        TLS_VERIFY TRUE
        INSTALL_DIR "${CMAKE_BINARY_DIR}/codecs"
        CMAKE_ARGS
            "-DCMAKE_INSTALL_PREFIX=<INSTALL_DIR>"
            -DCMAKE_INSTALL_LIBDIR=lib
            -DCMAKE_BUILD_TYPE=Release
            "-DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}"
            "-DCMAKE_MAKE_PROGRAM=${CMAKE_MAKE_PROGRAM}"
            ${ARGN}
    )
endfunction()

codec(jpeg "${JPEG_URL}" "${JPEG_SHA}" "${JPEG_ARCHIVE}"
    -DENABLE_SHARED=OFF -DENABLE_STATIC=ON -DWITH_JPEG8=ON -DWITH_SIMD=OFF
    -DWITH_TOOLS=OFF -DWITH_TESTS=OFF -DWITH_TURBOJPEG=OFF
)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    find_package(ZLIB REQUIRED)
else()
    codec(zlib "${ZLIB_URL}" "${ZLIB_SHA}" "${ZLIB_ARCHIVE}"
        -DZLIB_COMPAT=ON -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF
    )
endif()
codec(freetype "${FREETYPE_URL}" "${FREETYPE_SHA}" "${FREETYPE_ARCHIVE}"
    -DBUILD_SHARED_LIBS=OFF -DFT_DISABLE_ZLIB=ON -DFT_DISABLE_BZIP2=ON
    -DFT_DISABLE_PNG=ON -DFT_DISABLE_HARFBUZZ=ON -DFT_DISABLE_BROTLI=ON
)

set(draw_url "${PILLOW_URL}")
if(COMIC_SOL_CODEC_ARCHIVES)
    file(TO_CMAKE_PATH "${COMIC_SOL_CODEC_ARCHIVES}/${PILLOW_ARCHIVE}" draw_url)
endif()
set(draw_source "${CMAKE_BINARY_DIR}/pillow/src/libImaging")
ExternalProject_Add(pillow_draw
    URL "${draw_url}"
    URL_HASH "SHA256=${PILLOW_SHA}"
    DOWNLOAD_NAME "${PILLOW_ARCHIVE}"
    TLS_VERIFY TRUE
    SOURCE_DIR "${CMAKE_BINARY_DIR}/pillow"
    CONFIGURE_COMMAND ""
    BUILD_COMMAND ""
    INSTALL_COMMAND ""
)

set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/codecs/lib")
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY_RELEASE "${CMAKE_ARCHIVE_OUTPUT_DIRECTORY}")
set(draw_files "${draw_source}/Draw.c" "${draw_source}/Mode.c")
set_source_files_properties(${draw_files} PROPERTIES GENERATED TRUE)
add_library(comic_sol_raster STATIC codecs.c font.c masks.c ${draw_files})
add_dependencies(comic_sol_raster jpeg freetype pillow_draw)
set(archive_zlib_url "${ARCHIVE_ZLIB_URL}")
if(COMIC_SOL_CODEC_ARCHIVES)
    file(TO_CMAKE_PATH "${COMIC_SOL_CODEC_ARCHIVES}/${ARCHIVE_ZLIB_ARCHIVE}" archive_zlib_url)
endif()
set(archive_zlib "${CMAKE_BINARY_DIR}/archive-zlib")
ExternalProject_Add(archive_zlib_source
    URL "${archive_zlib_url}"
    URL_HASH "SHA256=${ARCHIVE_ZLIB_SHA}"
    DOWNLOAD_NAME "${ARCHIVE_ZLIB_ARCHIVE}"
    TLS_VERIFY TRUE
    SOURCE_DIR "${archive_zlib}"
    CONFIGURE_COMMAND ""
    BUILD_COMMAND ""
    INSTALL_COMMAND ""
)
set(archive_zlib_sources)
foreach(source adler32 crc32 deflate trees zutil)
    list(APPEND archive_zlib_sources "${archive_zlib}/${source}.c")
endforeach()
set_source_files_properties(${archive_zlib_sources} PROPERTIES GENERATED TRUE)
set_source_files_properties(${archive_zlib_sources} archive_zip.c PROPERTIES COMPILE_DEFINITIONS Z_PREFIX)
set_source_files_properties(archive_zip.c PROPERTIES INCLUDE_DIRECTORIES "${CMAKE_BINARY_DIR}")
target_sources(comic_sol_raster PRIVATE archive_zip.c ${archive_zlib_sources})
add_dependencies(comic_sol_raster archive_zlib_source)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_libraries(comic_sol_raster PRIVATE ZLIB::ZLIB)
else()
    add_dependencies(comic_sol_raster zlib)
endif()
target_compile_features(comic_sol_raster PRIVATE c_std_11)
target_include_directories(comic_sol_raster PRIVATE
    "${CMAKE_BINARY_DIR}/codecs/include"
    "${CMAKE_BINARY_DIR}/codecs/include/freetype2"
    "${draw_source}"
    "${CMAKE_CURRENT_SOURCE_DIR}/draw-types"
)

if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    codec(harfbuzz "${HARFBUZZ_URL}" "${HARFBUZZ_SHA}" "${HARFBUZZ_ARCHIVE}"
        -DBUILD_SHARED_LIBS=OFF -DHB_HAVE_FREETYPE=ON
        -DHB_BUILD_SUBSET=OFF -DHB_BUILD_RASTER=OFF -DHB_BUILD_VECTOR=OFF -DHB_BUILD_GPU=OFF
        "-DCMAKE_PREFIX_PATH=${CMAKE_BINARY_DIR}/codecs"
    )
    ExternalProject_Add_StepDependencies(harfbuzz configure freetype)
    set(thirdparty "${draw_source}/../thirdparty")
    set(layout_sources "${thirdparty}/raqm/raqm.c" "${thirdparty}/fribidi-shim/fribidi.c")
    set_source_files_properties(${layout_sources} PROPERTIES GENERATED TRUE)
    target_sources(comic_sol_raster PRIVATE ${layout_sources})
    add_dependencies(comic_sol_raster harfbuzz)
    target_compile_definitions(comic_sol_raster PRIVATE COMIC_SOL_RAQM)
    target_include_directories(comic_sol_raster PRIVATE
        "${thirdparty}"
        "${CMAKE_BINARY_DIR}/codecs/include/harfbuzz"
    )
endif()
