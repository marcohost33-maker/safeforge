#!/bin/sh
# Build the pinned LazyFS used by tests/test_powerloss_lazyfs.py.
#
#   experiments/lazyfs/build_lazyfs.sh <workdir>
#   SAFEFORGE_LAZYFS_BIN=<workdir>/lazyfs/lazyfs/build/lazyfs python -m pytest tests/test_powerloss_lazyfs.py
#
# Needs: git, cmake, g++, pkg-config, libfuse3-dev, fuse3 (Ubuntu/Debian
# package names). Every source is pinned to a commit; the upstream build
# downloads spdlog as a release tarball and toml11 by tag, which this script
# replaces with checked-out commits (FETCHCONTENT_SOURCE_DIR_*).
set -eu

LAZYFS_SHA=fa7d32ee9710d55a7e529ac3c375f6616563c78b   # dsrhaslab/lazyfs main, 2026-08-24
SPDLOG_SHA=76fb40d95455f249bd70824ecfcae7a8f0930fa3   # gabime/spdlog v1.10.0
TOML11_SHA=dcfe39a783a94e8d52c885e5883a6fbb21529019   # ToruNiina/toml11 v3.7.1

work=${1:?usage: build_lazyfs.sh <workdir>}
mkdir -p "$work"
cd "$work"

fetch() { # url dir sha
    if [ ! -d "$2/.git" ]; then
        git init -q "$2"
        git -C "$2" remote add origin "$1"
    fi
    git -C "$2" fetch -q --depth 1 origin "$3"
    git -C "$2" checkout -q --detach FETCH_HEAD
    test "$(git -C "$2" rev-parse HEAD)" = "$3"
}

fetch https://github.com/dsrhaslab/lazyfs.git lazyfs "$LAZYFS_SHA"
fetch https://github.com/gabime/spdlog.git spdlog "$SPDLOG_SHA"
fetch https://github.com/ToruNiina/toml11.git toml11 "$TOML11_SHA"

pc="$PWD/lazyfs/libs/libpcache/build"
cmake -S lazyfs/libs/libpcache -B "$pc" \
    -DFETCHCONTENT_SOURCE_DIR_SPDLOG="$PWD/spdlog" \
    -DFETCHCONTENT_SOURCE_DIR_TOML="$PWD/toml11" \
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON
cmake --build "$pc" -j"$(nproc)"

# lazyfs/CMakeLists.txt includes spdlog from libpcache's _deps/spdlog-src,
# which FETCHCONTENT_SOURCE_DIR_* leaves empty.
mkdir -p "$pc/_deps"
ln -sfn "$PWD/spdlog" "$pc/_deps/spdlog-src"

cmake -S lazyfs/lazyfs -B lazyfs/lazyfs/build -DLAZYFS_BUILD_TESTS=OFF
cmake --build lazyfs/lazyfs/build -j"$(nproc)"
echo "$PWD/lazyfs/lazyfs/build/lazyfs"
