#!/usr/bin/env bash
# axl-firmware: prebuilt UEFI firmware for QEMU (OVMF for X64 q35, ArmVirtQemu
# for AARCH64 virt), built from a pinned edk2 stable tag with QEMU's own
# build flags and this repository's patches, checked, and packed as a
# release. See README.md.
#
#   build.sh build [--edk2 DIR] [--install] [--prefix DIR]
#       Build both arches in both flavours (DEBUG, RELEASE), check every
#       property (static and booted), and pack dist/: the release tarball,
#       a symbols tarball per arch, SHA256SUMS. --install then installs
#       that tarball, as `install` does.
#   build.sh install --from <tarball|release-tag> [--prefix DIR] [--sha256 HEX] [--symbols]
#       Verify a release's sha256 and install it (lib/axlfw/install.py).
#   build.sh check <release-dir>
#       Re-run every image and boot check on an unpacked release.
#   build.sh verify <x64|aarch64> <code-image>
#       Only the FV check of one image: exit 0, or 1 naming what is wrong.
#   build.sh check-source <dir>
#       Only check an edk2 tree as a build checks its source.
#
# The edk2 source is, by default, fetched once into
#   ${XDG_CACHE_HOME:-~/.cache}/axl-firmware/edk2/<tag>
# (a blob-less clone of the tag, with the submodules the build needs), and
# made read-only. --edk2 DIR builds against another tree instead, which
# must be the same pinned commit, as committed. Either way the build is
# out of tree: edk2's WORKSPACE is a directory of this repository's own
# (work/), and PACKAGES_PATH is a patched copy of only the packages the
# patches touch, then the source. BaseTools is copied into the workspace
# and built there. Nothing is written into the source tree; a build that
# changed what `git status --ignored` says of it fails.
#
# Needs git, python3 (3.9 or later; 3.10 or later for the prebuilt
# uefi_firmware wheel), make, gcc, libuuid's headers (libuuid-devel or
# uuid-dev), nasm, iasl, aarch64-linux-gnu-gcc (or GCC_AARCH64_PREFIX),
# flock, xz, and QEMU (qemu-system-x86_64 and qemu-system-aarch64, from
# QEMU_DIR or PATH) for the boot checks. The build runs at nice 19 with
# JOBS (default 4) jobs.
set -euo pipefail

tag="edk2-stable202602"
commit=b7a715f7c03c45c6b4575bf88596bfd79658b8ce
url=https://github.com/tianocore/edk2.git
# "OvmfPkg/VirtioRngDxe: protect VirtioRngGetRNG against timer callbacks".
fix=8ba02634ecaa6ff5d0edcea266198f42eca90c53
# This repository's release of that tag: the release tag is <tag>-r<N>.
revision=1
release="$tag-r$revision"
repo=aximcode/axl-firmware

here=$(cd "$(dirname "$0")" && pwd)
prog=build.sh

die() {
    echo "$prog: $*" >&2
    exit 1
}

usage() {
    cat >&2 <<EOF
usage: $prog build [--edk2 DIR] [--install] [--prefix DIR]
       $prog install --from <tarball|release-tag> [--prefix DIR] [--sha256 HEX] [--symbols]
       $prog check <release-dir>
       $prog verify <x64|aarch64> <code-image>
       $prog check-source <dir>
EOF
    exit 2
}

# xdg <VAR> <default>: an XDG base directory; one that is not absolute is
# ignored, as the spec says.
xdg() {
    local v=${!1:-}
    [[ "$v" == /* ]] || v=$2
    echo "$v"
}

# The submodules the build needs, in the tree at <dir>: all but the
# unit-test frameworks'.
submodules() {
    git -C "$1" config -f .gitmodules --get-regexp '^submodule\..*\.path$' |
        awk '$2 !~ /^UnitTestFrameworkPkg\// { print $2 }'
}

# source_ok <dir>: the tree is the pinned commit, as committed, with the
# fix, and holds no file git would not account for.
source_ok() {
    local d=$1 head tagged dirty hidden sub
    [[ -d "$d/.git" || -f "$d/.git" ]] || {
        echo "$prog: $d: not a git checkout" >&2
        return 1
    }
    head=$(git -C "$d" rev-parse HEAD 2>/dev/null) || return 1
    tagged=$(git -C "$d" rev-parse "refs/tags/$tag^{commit}" 2>/dev/null) || tagged=none
    if [[ "$head" != "$commit" || "$tagged" != "$commit" ]]; then
        echo "$prog: $d: HEAD $head, $tag $tagged; pinned $commit" >&2
        return 1
    fi
    # Entries marked assume-unchanged or skip-worktree (or sparse) are
    # hidden from git status: none may be.
    hidden=$(
        git -C "$d" ls-files -v | grep -v '^H '
        # shellcheck disable=SC2016
        git -C "$d" submodule foreach --quiet \
            'git ls-files -v | sed -n "/^H /!s|^|$sm_path: |p"'
    ) || return 1
    if [[ -n "$hidden" ]]; then
        echo "$prog: $d: files hidden from git status:" >&2
        head -5 <<<"$hidden" >&2
        return 1
    fi
    # Tracked files as committed, and no untracked file that is not
    # ignored (a build's ignored output, Build/ or Conf/, is not read).
    dirty=$(git -C "$d" status --porcelain --ignore-submodules=none) || return 1
    if [[ -n "$dirty" ]]; then
        echo "$prog: $d: files changed or untracked:" >&2
        head -20 <<<"$dirty" >&2
        return 1
    fi
    # A leading ' ' is a submodule checked out at its recorded commit.
    sub=$(submodules "$d" | (cd "$d" && xargs git submodule status --)) || return 1
    if grep -v '^ ' <<<"$sub" | grep -q .; then
        echo "$prog: $d: submodules not at their recorded commits:" >&2
        grep -v '^ ' <<<"$sub" >&2
        return 1
    fi
    if ! git -C "$d" merge-base --is-ancestor "$fix" HEAD; then
        echo "$prog: $d: lacks the VirtioRng fix $fix" >&2
        return 1
    fi
}

# tree_state <dir>: what git says of every file, ignored ones included, in
# the tree and its submodules; a build must leave it unchanged.
tree_state() {
    git -C "$1" status --porcelain --ignored --ignore-submodules=none
    # shellcheck disable=SC2016
    git -C "$1" submodule foreach --quiet --recursive \
        'git status --porcelain --ignored | sed "s|^|$sm_path: |"'
}

# venv: a Python with requirements.txt's packages, kept in the cache by the
# file's digest. Sets py.
venv() {
    local cache want
    cache=$(xdg XDG_CACHE_HOME "$HOME/.cache")/axl-firmware
    want=$(sha256sum "$here/requirements.txt" | cut -c1-16)
    py="$cache/venv-$want/bin/python"
    # .complete is written last: a venv without it was cut short.
    [[ -f "$cache/venv-$want/.complete" ]] && return
    mkdir -p "$cache"
    rm -rf "$cache/venv-$want"
    python3 -m venv "$cache/venv-$want"
    "$py" -m pip install --quiet --disable-pip-version-check \
        --require-hashes -r "$here/requirements.txt" || die "cannot install requirements.txt"
    : >"$cache/venv-$want/.complete"
}

# axlfw <python> <args...>: the lib/axlfw package's command line (its
# launcher puts lib/ on the path; the working directory is kept).
axlfw() {
    local python=$1
    shift
    "$python" -E -s "$here/lib/axlfw-run.py" "$@"
}

case "${1:-}" in
    verify)
        (($# == 3)) || usage
        case "$2" in
            x64 | aarch64) ;;
            *) die "verify: arch is x64 or aarch64, not '$2'" ;;
        esac
        [[ -f "$3" ]] || die "$2: no image at $3"
        venv
        axlfw "$py" verify "$2" "$3"
        exit
        ;;
    check-source)
        (($# == 2)) || usage
        source_ok "$2" || exit 1
        echo "$2: $tag at $commit, as committed"
        exit
        ;;
    install)
        shift
        command -v python3 >/dev/null || die "python3 not found on PATH"
        axlfw python3 install --repo "$repo" "$@"
        exit
        ;;
    check)
        (($# == 2)) || usage
        venv
        axlfw "$py" check --release-dir "$(cd "$2" && pwd)" ${QEMU_DIR:+--qemu-dir "$QEMU_DIR"}
        exit
        ;;
    build) shift ;;
    *) usage ;;
esac

edk2="" install=0 prefix=""
while (($#)); do
    case "$1" in
        --edk2)
            (($# >= 2)) || usage
            edk2=$(cd "$2" && pwd) || die "--edk2: no directory $2"
            shift 2
            ;;
        --install)
            install=1
            shift
            ;;
        --prefix)
            (($# >= 2)) || usage
            prefix=$2
            shift 2
            ;;
        *) usage ;;
    esac
done

jobs=${JOBS:-4}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || die "JOBS must be a positive number, not '$jobs'"
aa64_prefix=${GCC_AARCH64_PREFIX:-aarch64-linux-gnu-}
aa64_gcc="${aa64_prefix}gcc"
for tool in git python3 make gcc nasm iasl flock truncate sha256sum xz "$aa64_gcc"; do
    command -v "$tool" >/dev/null || die "$tool not found on PATH"
done
for q in qemu-system-x86_64 qemu-system-aarch64; do
    if [[ -n "${QEMU_DIR:-}" ]]; then
        [[ -x "$QEMU_DIR/$q" ]] || die "QEMU_DIR=$QEMU_DIR has no $q"
    else
        command -v "$q" >/dev/null || die "$q not found on PATH (or set QEMU_DIR)"
    fi
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' ||
    die "python3 is $(python3 -V 2>&1); 3.9 or later is needed"
printf '#include <uuid/uuid.h>\n' | gcc -E - >/dev/null 2>&1 ||
    die "libuuid's headers (uuid/uuid.h) not found: install libuuid-devel or uuid-dev"
venv

cache=$(xdg XDG_CACHE_HOME "$HOME/.cache")/axl-firmware/edk2
work="$here/work"
mkdir -p "$cache" "$work"
# One build at a time per checkout, and per cache.
exec 8>"$work/.lock"
if ! flock -n 8; then
    echo "$prog: another build holds $work/.lock; waiting" >&2
    flock 8
fi

fetch_tmp=""
trap '[[ -z "$fetch_tmp" ]] || { chmod -R u+w "$fetch_tmp"; rm -rf "$fetch_tmp"; }' EXIT

fetch_source() {
    fetch_tmp=$(mktemp -d "$cache/.$tag.XXXXXX")
    local t="$fetch_tmp/edk2"
    if [[ -n "${EDK2_SRC:-}" ]]; then
        git -C "$EDK2_SRC" rev-parse --git-dir >/dev/null ||
            die "EDK2_SRC=$EDK2_SRC is not a git clone"
        echo "+ seeding the edk2 cache from $EDK2_SRC"
        git clone --quiet --no-checkout --no-hardlinks "$EDK2_SRC" "$t"
        git -C "$t" remote set-url origin "$url"
        git -C "$t" fetch --quiet --no-tags origin "+refs/tags/$tag:refs/tags/$tag"
    else
        echo "+ fetching edk2 $tag from $url"
        git clone --quiet --filter=blob:none --no-checkout --single-branch \
            --branch "$tag" "$url" "$t"
    fi
    git -C "$t" -c advice.detachedHead=false checkout --quiet --detach "$commit"
    submodules "$t" | (cd "$t" && xargs git submodule update --init --depth 1 --jobs 4 --)
    source_ok "$t" || die "the fetched edk2 does not check out (above)"
    if [[ -e "$src" ]]; then
        chmod -R u+w "$src"
        rm -rf "$src"
    fi
    mv "$t" "$src"
    rm -rf "$fetch_tmp"
    fetch_tmp=""
    # Read-only from here on: no build writes into its source. (Renaming a
    # directory needs it writable, so this comes after the rename.)
    chmod -R a-w "$src"
}

if [[ -n "$edk2" ]]; then
    src=$edk2
    source_ok "$src" || die "--edk2 $src: not the pinned source, as committed (above)"
    echo "+ building against $src, out of tree"
else
    src="$cache/$tag"
    exec 9>"$cache/.$tag.lock"
    if ! flock -n 9; then
        echo "$prog: another run holds $cache/.$tag.lock; waiting" >&2
        flock 9
    fi
    if source_ok "$src"; then
        echo "+ reusing the edk2 cache $src"
    else
        [[ ! -e "$src" ]] || echo "$prog: the edk2 cache $src does not check out; fetching again" >&2
        fetch_source
    fi
fi
before=$(tree_state "$src")

# QEMU's roms/edk2-build.config (v10.0.0), [opts.common]: every image.
common_opts=(
    NETWORK_HTTP_BOOT_ENABLE=TRUE
    NETWORK_IP6_ENABLE=TRUE
    NETWORK_TLS_ENABLE=TRUE
    NETWORK_ISCSI_ENABLE=TRUE
    NETWORK_ALLOW_HTTP_CONNECTIONS=TRUE
    TPM2_ENABLE=TRUE
    TPM2_CONFIG_ENABLE=TRUE
    TPM1_ENABLE=TRUE
    CAVIUM_ERRATUM_27456=TRUE
)
# [build.armvirt.aa64] adds [opts.armvirt.silent] and [pcds.nx.broken.shim.grub].
aa64_opts=(DEBUG_PRINT_ERROR_LEVEL=0x80000000)
aa64_pcds=(
    PcdDxeNxMemoryProtectionPolicy=0xC000000000007FD1
    PcdUninstallMemAttrProtocol=TRUE
)
# Ours, on both, so the front page is edk2-stable202408's, which QEMU
# 10.0.0 bundles: the Boot Manager Menu is UiApp
# (462CAA21-7614-4503-836E-8AB6F4662331), the front page, where edk2 now
# has BootManagerMenuApp (9d9e3a2b, 4e19daa8), so a Shell's `exit` lands
# there; and the front page's own modes, 80x25 and 640x480.
common_pcds=(
    "PcdBootManagerMenuFile=H{0x21,0xaa,0x2c,0x46,0x14,0x76,0x03,0x45,0x83,0x6e,0x8a,0xb6,0xf4,0x66,0x23,0x31}"
    PcdSetupConOutColumn=80
    PcdSetupConOutRow=25
    PcdSetupVideoHorizontalResolution=640
    PcdSetupVideoVerticalResolution=480
)
toolchain=GCC5
flavours=(DEBUG RELEASE)
# The firmware's version (SMBIOS type 0's BIOS version) is the tag, and its
# date the commit's, as QEMU's roms/Makefile passes them.
release_date=$(git -C "$src" log -1 --pretty=format:%cd --date=format:%m/%d/%Y "$commit")

# The workspace: BaseTools copied in and built there; the packages the
# patches touch copied and patched; Conf and Build of its own.
ws="$work/ws"
rm -rf "$ws"
mkdir -p "$ws/overlay" "$ws/Conf"
# copy_tracked <dir...>: the source's tracked files under each dir (their
# submodules' too) into the workspace's same place under $2's root.
copy_tracked() {
    local dest=$1
    shift
    (cd "$src" && git ls-files -z --recurse-submodules -- "$@") |
        tar -C "$src" --null -T - -cf - | tar -C "$dest" -xf -
    chmod -R u+w "$dest"
}
copy_tracked "$ws" BaseTools
mapfile -t patches < <(find "$here/patches" -maxdepth 1 -name '*.patch' | LC_ALL=C sort)
mapfile -t patched_pkgs < <(
    for p in "${patches[@]}"; do
        sed -n 's|^+++ b/\([^/]*\)/.*|\1|p' "$p"
    done | LC_ALL=C sort -u
)
if ((${#patched_pkgs[@]})); then
    copy_tracked "$ws/overlay" "${patched_pkgs[@]}"
fi
for p in "${patches[@]}"; do
    echo "+ applying patches/$(basename "$p") to the copy of ${patched_pkgs[*]}"
    # The overlay is no repository: keep git from finding this checkout's.
    GIT_CEILING_DIRECTORIES="$ws" git -C "$ws/overlay" apply --whitespace=nowarn "$p" ||
        die "patches/$(basename "$p") does not apply to $tag"
done

(
    cd "$ws"
    export WORKSPACE="$ws" PACKAGES_PATH="$ws/overlay:$src" EDK_TOOLS_PATH="$ws/BaseTools"
    export CONF_PATH="$ws/Conf" PYTHON_COMMAND=python3 PYTHONHASHSEED=1
    export GCC5_AARCH64_PREFIX="$aa64_prefix" GCC_AARCH64_PREFIX="$aa64_prefix"
    # BaseTools' C reads MdePkg's headers from EDK2_PATH (by default the
    # copy's ../../..): the source's, read only. Serially: VfrCompile's
    # generated parser races its own compile under -j (seen in CI as
    # "EfiVfrParser.cpp: expected initializer at end of input").
    echo "+ make -C BaseTools (the workspace's copy) EDK2_PATH=$src"
    nice -n 19 make -C BaseTools EDK2_PATH="$src" >"$ws/build-BaseTools.log" 2>&1 || {
        tail -40 "$ws/build-BaseTools.log" >&2
        die "the BaseTools build failed (log: $ws/build-BaseTools.log)"
    }
    # BuildEnv is not written for -e or -u; whether it worked is whether
    # `build` is on PATH after it.
    set +eu
    set --
    # shellcheck disable=SC1091
    . "$EDK_TOOLS_PATH/BuildEnv" >/dev/null
    set -eu
    [[ "$(command -v build)" == "$ws/BaseTools/BinWrappers/PosixLike/build" ]] ||
        die "BuildEnv did not put the workspace's BaseTools build first on PATH" \
            "(found: $(command -v build || echo none))"
    # edk2_build <target> <dsc> <arch> <opts...> -- <pcds...>
    edk2_build() {
        local target=$1 dsc=$2 arch=$3 a
        shift 3
        local -a cmd=(build -t "$toolchain" -b "$target" -p "$dsc" -a "$arch" -n "$jobs"
            --pcd "PcdFirmwareVersionString=L$tag\\0"
            --pcd "PcdFirmwareReleaseDateString=L$release_date\\0")
        while (($#)) && [[ "$1" != -- ]]; do
            cmd+=(-D "$1")
            shift
        done
        (($#)) && shift
        for a in "$@"; do
            cmd+=(--pcd "$a")
        done
        echo "+ ${cmd[*]}"
        nice -n 19 "${cmd[@]}" >"$ws/build-$arch-$target.log" 2>&1 || {
            tail -40 "$ws/build-$arch-$target.log" >&2
            die "the $arch $target build failed (log: $ws/build-$arch-$target.log)"
        }
    }
    for target in "${flavours[@]}"; do
        edk2_build "$target" OvmfPkg/OvmfPkgX64.dsc X64 "${common_opts[@]}" -- "${common_pcds[@]}"
        edk2_build "$target" ArmVirtPkg/ArmVirtQemu.dsc AARCH64 "${common_opts[@]}" "${aa64_opts[@]}" \
            -- "${common_pcds[@]}" "${aa64_pcds[@]}"
    done
)

after=$(tree_state "$src")
if [[ "$before" != "$after" ]]; then
    diff <(echo "$before") <(echo "$after") >&2 || true
    die "the build changed the source tree $src (above)"
fi
echo "+ the source tree is as it was: nothing written into $src"

# Stage, check, measure, describe and pack (lib/axlfw/release.py).
built_by=local
[[ -z "${GITHUB_ACTIONS:-}" ]] || built_by="github-actions ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
args=(assemble --ws "$ws" --src "$src" --dist "$here/dist" --stage "$work/stage"
    --release "$release" --tag "$tag" --commit "$commit" --url "$url" --fix "$fix"
    --toolchain "$toolchain" --release-date "$release_date"
    --compiler "x64=gcc $(gcc -dumpfullversion)"
    --compiler "aarch64=$aa64_gcc $("$aa64_gcc" -dumpfullversion)"
    --defines "x64=${common_opts[*]}" --defines "aarch64=${common_opts[*]} ${aa64_opts[*]}"
    --pcds "x64=${common_pcds[*]}" --pcds "aarch64=${common_pcds[*]} ${aa64_pcds[*]}"
    --repo-commit "$(git -C "$here" rev-parse HEAD 2>/dev/null || echo none)"
    --repo-dirty "$(git -C "$here" status --porcelain 2>/dev/null | wc -l)"
    --built-by "$built_by")
for p in "${patches[@]}"; do
    args+=(--patch "$p")
done
[[ -z "${QEMU_DIR:-}" ]] || args+=(--qemu-dir "$QEMU_DIR")
axlfw "$py" "${args[@]}"

if [[ $install == 1 ]]; then
    axlfw python3 install --repo "$repo" --from "$here/dist/axl-firmware-$release.tar.xz" \
        ${prefix:+--prefix "$prefix"}
fi
