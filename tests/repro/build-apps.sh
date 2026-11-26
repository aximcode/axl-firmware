#!/usr/bin/env bash
# build-apps.sh <x64|aarch64> <out-dir>
#
# Build the reproducers of patches 0011 and 0013 (ReproPkg: VfsRace.efi
# and TcpHold.efi) for one arch into <out-dir>, out of tree, with the
# BaseTools of the last `build.sh build` (work/ws/BaseTools) and the
# pinned edk2 source in the cache. X64 is built for baseline x86-64, as
# the firmware is (pin_x64_march). See repro.py and DESIGN §4.28-4.30.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
top=$(cd "$here/../.." && pwd)
(($# == 2)) || {
    echo "usage: $0 <x64|aarch64> <out-dir>" >&2
    exit 2
}
case "$1" in
    x64) arch=X64 ;;
    aarch64) arch=AARCH64 ;;
    *)
        echo "$0: arch is x64 or aarch64, not '$1'" >&2
        exit 2
        ;;
esac
mkdir -p "$2"
out=$(cd "$2" && pwd)
src=${EDK2_SRC_TREE:-${XDG_CACHE_HOME:-$HOME/.cache}/axl-firmware/edk2/edk2-stable202611}
tools=$top/work/ws/BaseTools
[[ -x $tools/BinWrappers/PosixLike/build ]] || {
    echo "$0: no built BaseTools at $tools (run build.sh build first)" >&2
    exit 1
}
ws=$(mktemp -d "${TMPDIR:-/tmp}/axlfw-repro.XXXXXX")
trap 'rm -rf "$ws"' EXIT
mkdir -p "$ws/Conf"
ln -s "$tools" "$ws/BaseTools"
cd "$ws"
export WORKSPACE="$ws" PACKAGES_PATH="$here:$src" EDK_TOOLS_PATH="$ws/BaseTools" CONF_PATH="$ws/Conf"
export PYTHON_COMMAND=python3 GCC_AARCH64_PREFIX=${GCC_AARCH64_PREFIX:-aarch64-linux-gnu-}
set +eu
set --
# shellcheck disable=SC1091
. "$EDK_TOOLS_PATH/BuildEnv" >/dev/null
set -eu
(
    # shellcheck source=/dev/null
    . "$top/build.sh"
    pin_x64_march "$CONF_PATH/tools_def.txt" -march=x86-64 DEBUG RELEASE
)
nice -n 10 build -t GCC -b DEBUG -p ReproPkg/ReproPkg.dsc -a "$arch" -n "${JOBS:-4}" >"$ws/build.log" 2>&1 || {
    tail -30 "$ws/build.log" >&2
    exit 1
}
cp "$ws/Build/Repro/DEBUG_GCC/$arch/VfsRace.efi" "$ws/Build/Repro/DEBUG_GCC/$arch/TcpHold.efi" "$out/"
echo "$out: VfsRace.efi TcpHold.efi ($arch)"
