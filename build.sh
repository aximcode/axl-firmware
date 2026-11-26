#!/usr/bin/env bash
# axl-firmware: prebuilt UEFI firmware for QEMU (OVMF for X64 q35, ArmVirtQemu
# for AARCH64 virt), built from a pinned edk2 stable tag with QEMU's own
# build flags and this repository's patches, checked, and packed as a
# release. See README.md and docs/DESIGN.md.
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
#   build.sh fetch
#       Only fetch the pinned source into the cache (below), or check the
#       cache there.
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
# JOBS jobs (default: the CPUs this process may use, `nproc`; CI sets 4).
set -euo pipefail

tag="edk2-stable202611"
commit=556dcfb466e9f05affeaff881d738807f8156d3c
url=https://github.com/tianocore/edk2.git
# The fixes every build must hold, each an ancestor of the pin:
#  - 8ba02634 "OvmfPkg/VirtioRngDxe: protect VirtioRngGetRNG against timer
#    callbacks" (boots could stall);
#  - b8df7d9c "ArmVirtPkg/ArmPlatformLibQemu: Support early ID map on LPA2
#    capable CPUs" (edk2 issue #11962: AARCH64 hung after its banner on a
#    CPU with FEAT_LPA2, such as QEMU's -cpu max, from 202511 to 202602).
fixes=(8ba02634ecaa6ff5d0edcea266198f42eca90c53 b8df7d9c8e1e370b862890ebda677278c4c6155f)
# This repository's release of that tag: the release tag is <tag>-r<N>.
revision=3
release="$tag-r$revision"
repo=aximcode/axl-firmware

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
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
       $prog fetch
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

# untracked <dir>: the files under dir that git does not track and the
# tree's own .gitignore files do not ignore, in the tree and its
# submodules, as "?? <path>". Neither the user's core.excludesFile nor
# .git/info/exclude hides one.
untracked() {
    local top subs
    top=$(git -C "$1" ls-files -o --exclude-per-directory=.gitignore) || return 1
    # shellcheck disable=SC2016
    subs=$(git -C "$1" submodule foreach --quiet --recursive \
        'out=$(git ls-files -o --exclude-per-directory=.gitignore) || exit 1
         [ -z "$out" ] || printf "%s\n" "$out" | sed "s|^|$displaypath/|"') || return 1
    printf '%s\n%s\n' "$top" "$subs" | sed -n 's/^./?? &/p'
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
    hidden=$(git -C "$d" ls-files -v) || return 1
    hidden=$(grep -v '^H ' <<<"$hidden") || hidden=""
    # shellcheck disable=SC2016
    sub=$(git -C "$d" submodule foreach --quiet \
        'out=$(git ls-files -v) || exit 1
         [ -z "$out" ] || printf "%s\n" "$out" | sed -n "/^H /!s|^|$sm_path: |p"') || return 1
    hidden=$(printf '%s\n%s\n' "$hidden" "$sub" | grep .) || hidden=""
    if [[ -n "$hidden" ]]; then
        echo "$prog: $d: files hidden from git status:" >&2
        head -5 <<<"$hidden" >&2
        return 1
    fi
    # Tracked files as committed (a submodule with an untracked file shows
    # here too, as modified), and no untracked file that the tree's
    # .gitignore does not ignore (a build's ignored output, Build/ or
    # Conf/, is not read).
    dirty=$(git -C "$d" status --porcelain --untracked-files=no --ignore-submodules=none) || return 1
    sub=$(untracked "$d") || return 1
    dirty=$(printf '%s\n%s\n' "$dirty" "$sub" | grep .) || dirty=""
    if [[ -n "$dirty" ]]; then
        echo "$prog: $d: files changed or untracked:" >&2
        head -20 <<<"$dirty" >&2
        return 1
    fi
    # A leading ' ' is a submodule checked out at its recorded commit.
    sub=$(submodules "$d" | (cd "$d" && xargs git submodule status --)) || return 1
    # Not `grep | grep -q`: under pipefail, grep -q's early exit can fail the
    # producer (SIGPIPE, or EPIPE where it is ignored) and so the test.
    sub=$(grep -v '^ ' <<<"$sub") || sub=""
    if [[ -n "$sub" ]]; then
        echo "$prog: $d: submodules not at their recorded commits:" >&2
        printf '%s\n' "$sub" >&2
        return 1
    fi
    local f
    for f in "${fixes[@]}"; do
        if ! git -C "$d" merge-base --is-ancestor "$f" HEAD; then
            echo "$prog: $d: lacks the fix $f" >&2
            return 1
        fi
    done
}

# tree_state <dir>: what git says of every file, ignored ones included, in
# the tree and its submodules; a build must leave it unchanged.
tree_state() {
    git -C "$1" status --porcelain --ignored --ignore-submodules=none || return 1
    # shellcheck disable=SC2016
    git -C "$1" submodule foreach --quiet --recursive \
        'out=$(git status --porcelain --ignored) || exit 1
         [ -z "$out" ] || printf "%s\n" "$out" | sed "s|^|$sm_path: |"' || return 1
}

# venv: a Python with requirements.txt's packages, kept in the cache by the
# file's digest. Sets py.
venv() {
    local cache want
    cache=$(xdg XDG_CACHE_HOME "$HOME/.cache")/axl-firmware
    want=$(sha256sum "$here/requirements.txt") || die "cannot read $here/requirements.txt"
    want=${want:0:16}
    py="$cache/venv-$want/bin/python"
    # .complete is written last: a venv without it was cut short.
    [[ -f "$cache/venv-$want/.complete" ]] && return
    mkdir -p "$cache"
    # One run makes it; another waits, then finds it complete.
    # (errexit is off in a subshell tested by ||: each step says its failure.)
    (
        flock 7 || die "cannot lock $cache/.venv-$want.lock"
        [[ ! -f "$cache/venv-$want/.complete" ]] || exit 0
        rm -rf "$cache/venv-$want" || die "cannot remove $cache/venv-$want"
        python3 -m venv "$cache/venv-$want" || die "python3 -m venv failed (python3-venv installed?)"
        "$py" -m pip install --quiet --disable-pip-version-check \
            --require-hashes -r "$here/requirements.txt" || die "cannot install requirements.txt"
        : >"$cache/venv-$want/.complete" || die "cannot write $cache/venv-$want/.complete"
    ) 7>"$cache/.venv-$want.lock" || exit 1
}

# repo_facts: this checkout's commit and how many files differ from it, as
# the manifest records them; "none" and -1 when git cannot tell (no
# checkout, or one git refuses for its ownership). Sets repo_commit and
# repo_dirty.
repo_facts() {
    local st
    repo_commit=$(git -C "$here" rev-parse HEAD 2>/dev/null) || repo_commit=none
    if st=$(git -C "$here" status --porcelain 2>/dev/null); then
        repo_dirty=$(grep -c . <<<"$st") || repo_dirty=0
    else
        repo_dirty=-1
    fi
}

# axlfw <python> <args...>: the lib/axlfw package's command line (its
# launcher puts lib/ on the path; the working directory is kept).
axlfw() {
    local python=$1
    shift
    "$python" -E -s "$here/lib/axlfw-run.py" "$@"
}

# default_jobs: the build's parallelism when JOBS is unset: the CPUs this
# process may use (nproc honours its CPU affinity), or 4 without nproc.
default_jobs() {
    local n
    if n=$(nproc 2>/dev/null) && [[ "$n" =~ ^[1-9][0-9]*$ ]]; then
        echo "$n"
    else
        echo 4
    fi
}

# pin_x64_march <tools_def> <march> <target...>: <march> appended to each
# flavour's X64 CC_FLAGS in the workspace's own tools_def.txt (BuildEnv's
# copy of the template), so that X64 code is built for baseline x86-64
# (DESIGN §4.24). edk2's GCC X64 flags name no -march, so gcc's own default
# decides, and Ubuntu's amd64v3 gcc (GitHub's runners) and AlmaLinux 10's
# both default to x86-64-v3. Every X64 C compile of every module and library starts from
# these flags; the DSC and INF flags edk2 appends to them name no -march.
# With LTO the code is generated at the link from the options each object
# was compiled with, so the link needs none. Each target must have exactly
# one such line. release.py's ISA check reads the makefiles and the code.
pin_x64_march() {
    local def=$1 march=$2 target n cr=$'\r' re
    shift 2
    # march as an ERE, for the check below.
    re=$(printf '%s' "$march" | sed 's/[][\\.*^$+?(){}|]/\\&/g')
    # The template's lines end in CRLF, and edk2's parser (Python's
    # universal newlines) ends a line at a lone CR too: the flag goes
    # before the CR, or it is a line of its own, which the parser drops.
    awk -v march="$march" '
        /^[[:blank:]]*(DEBUG|RELEASE|NOOPT)_GCC_X64_CC_FLAGS[[:blank:]]*=/ {
            cr = sub(/\r$/, "")
            end = cr ? "\r" : ""
            $0 = $0 " " march end
        }
        { print }' "$def" >"$def.new" || die "cannot rewrite $def"
    mv "$def.new" "$def"
    for target; do
        n=$(grep -cE "^[[:blank:]]*${target}_GCC_X64_CC_FLAGS[[:blank:]]*=[^$cr]* $re$cr?\$" "$def") || n=0
        [[ $n == 1 ]] || die "$def: $n ${target}_GCC_X64_CC_FLAGS lines to pin $march on, not 1"
    done
}

# Sourced (by the tests): the functions above, and nothing run.
[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0

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
        rd=$(cd "$2" 2>/dev/null && pwd) || die "check: no directory $2"
        axlfw "$py" check --release-dir "$rd" ${QEMU_DIR:+--qemu-dir "$QEMU_DIR"}
        exit
        ;;
    build) shift ;;
    fetch)
        (($# == 1)) || usage
        fetch_only=1
        shift
        ;;
    *) usage ;;
esac

fetch_only=${fetch_only:-0}
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

jobs=${JOBS:-$(default_jobs)}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || die "JOBS must be a positive number, not '$jobs'"
aa64_prefix=${GCC_AARCH64_PREFIX:-aarch64-linux-gnu-}
aa64_gcc="${aa64_prefix}gcc"
for tool in git flock; do
    command -v "$tool" >/dev/null || die "$tool not found on PATH"
done

cache=$(xdg XDG_CACHE_HOME "$HOME/.cache")/axl-firmware/edk2
work="$here/work"
mkdir -p "$cache" "$work"
# One build at a time per checkout, and per cache.
exec 8>"$work/.lock"
if ! flock -n 8; then
    echo "$prog: another build holds $work/.lock; waiting" >&2
    flock 8
fi

if [[ $fetch_only == 0 ]]; then
    # An earlier build's release goes first: a build that fails leaves none
    # that would look like its own.
    rm -f "$here"/dist/axl-firmware-* "$here/dist/SHA256SUMS"
    for tool in python3 make gcc objdump nasm iasl truncate sha256sum xz "$aa64_gcc"; do
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
    x64_cc=$(gcc -dumpfullversion) || die "gcc -dumpfullversion failed"
    aa64_cc=$("$aa64_gcc" -dumpfullversion) || die "$aa64_gcc -dumpfullversion failed"
    repo_facts
fi

fetch_tmp=""
trap '[[ -z "$fetch_tmp" ]] || { chmod -R u+w "$fetch_tmp"; rm -rf "$fetch_tmp"; }' EXIT

fetch_source() {
    fetch_tmp=$(mktemp -d "$cache/.$tag.XXXXXX") || die "cannot make a directory in $cache"
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
    if [[ -e "$src" ]] && source_ok "$src"; then
        echo "+ reusing the edk2 cache $src"
    else
        [[ ! -e "$src" ]] || echo "$prog: the edk2 cache $src does not check out; fetching again" >&2
        fetch_source
    fi
fi
if [[ $fetch_only == 1 ]]; then
    echo "$src: $tag at $commit, as committed"
    exit
fi
before=$(tree_state "$src") || die "cannot read what git says of $src"
touch "$work/.source-stamp"

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
# Ours, AARCH64 only: the serial console's terminal type is VT-UTF8, as
# X64's (OVMF's PlatformBootManagerLib names VenUtf8 in its console paths),
# where ArmVirtQemu.dsc says VT100: so DEL (0x7f) is Backspace
# (TerminalConIn.c) and box drawing goes out as UTF-8 (DESIGN §4.29).
# PcdTerminalTypeGuidBuffer is the type node PlatformBootManagerLibLight puts
# in the console paths (EFI_VT_UTF8_GUID, AD15A0D6-8BEC-4ACF-A073-D01DE77E2D88);
# PcdDefaultTerminalType (3, VT-UTF8) the type TerminalDxe takes for a port
# connected without one, such as a PCI 16550's.
aa64_pcds+=(
    PcdDefaultTerminalType=3
    "PcdTerminalTypeGuidBuffer=H{0xd6,0xa0,0x15,0xad,0xec,0x8b,0xcf,0x4a,0xa0,0x73,0xd0,0x1d,0xe7,0x7e,0x2d,0x88}"
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
# edk2's GCC toolchain tag. QEMU 10.0.0's config says GCC5, which
# edk2-stable202611's tools_def removed (3.06: "Remove GCC48, GCC49 and
# GCC5"); GCC is its successor.
toolchain=GCC
flavours=(DEBUG RELEASE)
# The X64 code's instruction set: baseline x86-64 (DESIGN §4.24).
x64_march=-march=x86-64
# The firmware's version (SMBIOS type 0's BIOS version) is the tag, and its
# date the commit's, as QEMU's roms/Makefile passes them.
release_date=$(git -C "$src" log -1 --pretty=format:%cd --date=format:%m/%d/%Y "$commit") ||
    die "cannot read the date of $commit in $src"

# The workspace: BaseTools copied in and built there; the packages the
# patches touch copied and patched; Conf and Build of its own.
ws="$work/ws"
# A run cut short may leave read-only files copied from the source.
[[ ! -e "$ws" ]] || chmod -R u+w "$ws"
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
# (A process substitution's failure is not seen: an empty list is.)
((${#patches[@]} > 0)) || die "no patches found in $here/patches"
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
    export GCC_AARCH64_PREFIX="$aa64_prefix"
    # edk2's build stamps its output with SOURCE_DATE_EPOCH when set: the
    # pinned commit's date, so that the time of the build is not in it.
    SOURCE_DATE_EPOCH=$(git -C "$src" log -1 --pretty=format:%ct "$commit") ||
        die "cannot read the date of $commit in $src"
    export SOURCE_DATE_EPOCH
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
    pin_x64_march "$CONF_PATH/tools_def.txt" "$x64_march" "${flavours[@]}"
    echo "+ X64 C compiled for baseline x86-64: $x64_march on the X64 CC_FLAGS of $CONF_PATH/tools_def.txt"
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
        # edk2's build report: the modules in each firmware volume (FLASH)
        # and the libraries linked into each (LIBRARY), from which each
        # image's licence list is derived (lib/axlfw/licenses.py). It is
        # written after the images and does not change their bytes
        # (DESIGN §4.22).
        cmd+=(-y "$ws/report-$arch-$target.txt" -Y LIBRARY -Y FLASH)
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

after=$(tree_state "$src") || die "cannot read what git says of $src"
if [[ "$before" != "$after" ]]; then
    diff <(echo "$before") <(echo "$after") >&2 || true
    die "the build changed the source tree $src (above)"
fi
# git collapses an ignored directory to one line and never sees a change
# inside it (a Build/ or Conf/ of the tree's own): any file or directory
# modified since the stamp, outside .git, is a write.
touched=$(find "$src" -path '*/.git' -prune -o -newer "$work/.source-stamp" -print) ||
    die "cannot read all of $src to check that nothing was written into it"
if [[ -n "$touched" ]]; then
    head -20 <<<"$touched" >&2
    die "the build wrote into the source tree $src (above)"
fi
echo "+ the source tree is as it was: nothing written into $src"

# Stage, check, measure, describe and pack (lib/axlfw/release.py).
built_by=local
[[ -z "${GITHUB_ACTIONS:-}" ]] || built_by="github-actions ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
args=(assemble --ws "$ws" --src "$src" --dist "$here/dist" --stage "$work/stage"
    --release "$release" --tag "$tag" --commit "$commit" --url "$url"
    --toolchain "$toolchain" --release-date "$release_date"
    --compiler "x64=gcc $x64_cc" --compiler "aarch64=$aa64_gcc $aa64_cc"
    --defines "x64=${common_opts[*]}" --defines "aarch64=${common_opts[*]} ${aa64_opts[*]}"
    --pcds "x64=${common_pcds[*]}" --pcds "aarch64=${common_pcds[*]} ${aa64_pcds[*]}"
    --repo-commit "$repo_commit" --repo-dirty "$repo_dirty"
    --built-by "$built_by")
for p in "${patches[@]}"; do
    args+=(--patch "$p")
done
for f in "${fixes[@]}"; do
    args+=(--fix "$f")
done
[[ -z "${QEMU_DIR:-}" ]] || args+=(--qemu-dir "$QEMU_DIR")
axlfw "$py" "${args[@]}"

if [[ $install == 1 ]]; then
    axlfw python3 install --repo "$repo" --from "$here/dist/axl-firmware-$release.tar.xz" \
        ${prefix:+--prefix "$prefix"}
fi
