#!/bin/sh
# Build a SQLite with the WAL-reset fix (>= 3.51.3) as libsqlite3.so.0 for
# tests/test_r40_sqlite_evidence.py and the release gate's no-waiver pass.
#
#   experiments/sqlite/build_sqlite.sh <workdir>
#   LD_LIBRARY_PATH=<workdir>/lib SAFEFORGE_EXPECT_FIXED_SQLITE=3.54.1 python -m pytest
#
# Why: no GitHub runner image links a fixed SQLite (run 40984016099,
# 2026-10-30: ubuntu 3.45.1, windows/macos 3.49.1 and 3.50.4), so without this
# the True path of durability_prerequisites_ok only ever ran against a pinned
# fake. Linux only: the setup-python build links the system libsqlite3.so.0
# dynamically, so LD_LIBRARY_PATH replaces it; the tests then assert the
# version actually loaded, so a library that did not take effect fails them.
#
# Source: the official GitHub mirror of SQLite, pinned to the commit tagged
# version-3.54.1 (git ls-remote https://github.com/sqlite/sqlite, 2026-10-30).
# A git commit id names the whole tree, so the pin is the integrity check.
# Needs: git, a C compiler, make, tclsh (Ubuntu: tcl) for the amalgamation.
set -eu

SQLITE_SHA=57520227c95396ce39ba6ecaac8d475b78dd3964   # sqlite/sqlite tag version-3.54.1
SQLITE_VERSION=3.54.1

work=${1:?usage: build_sqlite.sh <workdir>}
mkdir -p "$work"
cd "$work"

if [ ! -d src/.git ]; then
    git init -q src
    git -C src remote add origin https://github.com/sqlite/sqlite
fi
git -C src fetch -q --depth 1 origin "$SQLITE_SHA"
git -C src checkout -q --detach FETCH_HEAD
test "$(git -C src rev-parse HEAD)" = "$SQLITE_SHA"
test "$(cat src/VERSION)" = "$SQLITE_VERSION"

mkdir -p build lib
cd build
../src/configure --disable-tcl >configure.log
make sqlite3.c >make.log
# Options close to Debian/Ubuntu's libsqlite3 so CPython's _sqlite3 finds every symbol.
${CC:-cc} -O2 -fPIC -shared -Wl,-soname,libsqlite3.so.0 \
    -DSQLITE_THREADSAFE=1 \
    -DSQLITE_ENABLE_COLUMN_METADATA \
    -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS5 \
    -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_DBSTAT_VTAB \
    -DSQLITE_ENABLE_MATH_FUNCTIONS -DSQLITE_ENABLE_UNLOCK_NOTIFY \
    sqlite3.c -o ../lib/libsqlite3.so.0 -lm -ldl -lpthread
cd ..
grep -q "#define SQLITE_VERSION *\"$SQLITE_VERSION\"" build/sqlite3.h
echo "built $(pwd)/lib/libsqlite3.so.0 (SQLite $SQLITE_VERSION, $SQLITE_SHA)"
