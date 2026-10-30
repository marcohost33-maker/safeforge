#!/bin/sh
# Build a pinned SQLite as libsqlite3.so.0 for the real-library tests
# (tests/test_r40_sqlite_evidence.py) and the release gate.
#
#   experiments/sqlite/build_sqlite.sh <workdir> [version]     (default 3.54.1)
#   LD_LIBRARY_PATH=<workdir>/lib SAFEFORGE_EXPECT_SQLITE=<version> \
#       SAFEFORGE_EXPECT_SQLITE_FIXED=<true|false> python -m pytest
#
# Why: no GitHub runner image links a SQLite with the WAL-reset fix (run
# 40984016099, 2026-10-30: ubuntu 3.45.1, windows/macos 3.49.1 and 3.50.4), so
# without this the fixed path only ever ran against a pinned fake (R40). R41
# adds the boundary builds of the classifier in safeforge.sqlite_runtime: the
# last affected release, the first fixed one, and the two branch backports.
# Linux only: the setup-python build links the system libsqlite3.so.0
# dynamically, so LD_LIBRARY_PATH replaces it; the tests assert the library
# actually loaded, so one that did not take effect fails them.
#
# Pins (git ls-remote https://github.com/sqlite/sqlite, 2026-10-30): the git
# commit of the official mirror names the whole tree; manifest.uuid is SQLite's
# own Fossil check-in id, which the built library reports in sqlite_source_id().
# The fix column is checked against the CODE, not only the version number: the
# WAL-reset fix adds, in walCheckpoint() of src/wal.c, a comparison of the live
# wal-index salt with the header's before backfilling (diff 3.51.2..3.51.3).
# A build whose marker disagrees with the table fails here, so the version-based
# classifier and the source can never drift apart unnoticed.
# Needs: git, a C compiler, make, tclsh (Ubuntu: tcl) for the amalgamation.
set -eu

work=${1:?usage: build_sqlite.sh <workdir> [version]}
version=${2:-3.54.1}

#            version git commit (mirror)                         fix  Fossil check-in (manifest.uuid)
case "$version" in
3.54.1) sha=57520227c95396ce39ba6ecaac8d475b78dd3964 fix=1 checkin=555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76 ;;
3.51.3) sha=a5333afb9ad1aa473f8963b92caeaa955f47dc74 fix=1 checkin=737ae4a34738ffa0c3ff7f9bb18df914dd1cad163f28fd6b6e114a344fe6d618 ;;
3.51.2) sha=ba76c160c735437de974c6ea24c890a663214c6b fix=0 checkin=b270f8339eb13b504d0b2ba154ebca966b7dde08e40c3ed7d559749818cb2075 ;;
3.50.7) sha=b4d3df26bae9e83a6828b433adfdf002a812637a fix=1 checkin=c7facf7ac58d2cdac619a2da838c5fd5c5270f52b3458c7881b5a4fca1ca4791 ;; # branch-3.50 head
3.44.6) sha=66170da47a06b3fe6f20b96308438d5b7fa09fa0 fix=1 checkin=863c171b76cd36e0c71662d4a50d84da531cc7fbead893d02459577febdf6396 ;; # branch-3.44 head
*) echo "no pin for SQLite $version" >&2; exit 2 ;;
esac
marker='memcmp(pLive->aSalt, pWal->hdr.aSalt'

mkdir -p "$work"
cd "$work"

if [ ! -d src/.git ]; then
    git init -q src
    git -C src remote add origin https://github.com/sqlite/sqlite
fi
git -C src fetch -q --depth 1 origin "$sha"
git -C src checkout -q --detach FETCH_HEAD
test "$(git -C src rev-parse HEAD)" = "$sha"
test "$(cat src/VERSION)" = "$version"
test "$(cat src/manifest.uuid)" = "$checkin"
test "$(grep -cF "$marker" src/src/wal.c)" = "$fix"

rm -rf build lib
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
grep -q "#define SQLITE_VERSION *\"$version\"" build/sqlite3.h
grep -q "#define SQLITE_SOURCE_ID *\"[0-9: -]* $checkin\"" build/sqlite3.h
echo "built $(pwd)/lib/libsqlite3.so.0 (SQLite $version, git $sha, check-in $checkin, fix-marker $fix)"
