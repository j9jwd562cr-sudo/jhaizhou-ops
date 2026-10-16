#!/usr/bin/env bash
# Pin the PNG compressor used by the upstream frozen byte hashes.
set -euo pipefail
cd "$(dirname "$0")/.."
PNG_PREFIX="$PWD/.tools/png-zlib-ng-2.3.2"
if [[ ! -f "$PNG_PREFIX/lib/libz.so.1" ]]; then
  PNG_BUILD=$(mktemp -d)
  trap 'rm -rf "$PNG_BUILD"' EXIT
  curl -fL --retry 3 https://github.com/zlib-ng/zlib-ng/archive/6d9f3dc072369dc719a5fbe71d4e086a96a680bd.tar.gz -o "$PNG_BUILD/source.tar.gz"
  printf '%s  %s\n' 8f24a48f6a9325044941908cdbd6eb4852fce279ad3e4969a82d620fcf184e3a "$PNG_BUILD/source.tar.gz" | sha256sum -c -
  tar -xzf "$PNG_BUILD/source.tar.gz" -C "$PNG_BUILD"
  cmake -S "$PNG_BUILD/zlib-ng-6d9f3dc072369dc719a5fbe71d4e086a96a680bd" -B "$PNG_BUILD/build" \
    -DZLIB_COMPAT=ON -DBUILD_SHARED_LIBS=ON -DZLIB_ENABLE_TESTS=OFF -DWITH_GTEST=OFF \
    -DCMAKE_INSTALL_PREFIX="$PNG_PREFIX" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$PNG_BUILD/build" -j2
  cmake --install "$PNG_BUILD/build"
fi
test -f "$PNG_PREFIX/lib/libz.so.1"
