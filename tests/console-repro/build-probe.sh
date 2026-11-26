#!/usr/bin/env bash
# build-probe.sh <release-ws> <probe-ws>: Probe.efi (AxlConsoleReproPkg) for
# X64 and AARCH64, with the BaseTools and Conf of a finished `build.sh build`
# workspace (work/ws), into <probe-ws>/Build/AxlConsoleRepro/RELEASE_GCC/.
# The edk2 source is build.sh's cache, or EDK2_SRC.
set -euo pipefail
(($# == 2)) || { echo "usage: build-probe.sh <release-ws> <probe-ws>" >&2; exit 2; }
ws=$(cd "$1" && pwd)
mkdir -p "$2/Conf"
pws=$(cd "$2" && pwd)
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cache=${XDG_CACHE_HOME:-}
[[ "$cache" == /* ]] || cache="$HOME/.cache"
src=${EDK2_SRC:-$cache/axl-firmware/edk2/edk2-stable202611}
cp "$ws/Conf/"*.txt "$pws/Conf/"
cd "$pws"
export WORKSPACE="$pws" PACKAGES_PATH="$here:$src" EDK_TOOLS_PATH="$ws/BaseTools"
export CONF_PATH="$pws/Conf" PYTHON_COMMAND=python3 GCC_AARCH64_PREFIX=${GCC_AARCH64_PREFIX:-aarch64-linux-gnu-}
# BuildEnv is not written for -e or -u.
set +eu
set --
# shellcheck disable=SC1091
. "$EDK_TOOLS_PATH/BuildEnv" >/dev/null 2>&1
set -eu
for arch in X64 AARCH64; do
    nice -n 10 build -t GCC -b RELEASE -a "$arch" -n 2 -p AxlConsoleReproPkg/AxlConsoleReproPkg.dsc \
        >"$pws/build-$arch.log" 2>&1 || {
        tail -30 "$pws/build-$arch.log" >&2
        exit 1
    }
done
ls "$pws"/Build/AxlConsoleRepro/RELEASE_GCC/*/Probe.efi
