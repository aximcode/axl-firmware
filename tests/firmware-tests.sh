#!/usr/bin/env bash
# The checks of guest/build-firmware.sh (DESIGN.md §11 item 101) that need
# no build:
#   - its `--verify` mode against a fake fwtool (every driver there, and
#     each GUID asked for exactly once; two missing, named; fwtool failing
#     or naming no node; no fwtool; no image; bad arguments);
#   - its `--check-source` mode against a copy of the edk2 cache: the copy
#     as fetched passes; a tracked file changed, a file hidden from git
#     status (skip-worktree), a submodule not checked out, the tag moved,
#     and a directory that is no checkout each fail, said;
#   - real images, when axl-sdk's fwtool is here: the distro's OVMF
#     (Fedora/EL path), which lacks VirtioFsDxe and VirtioGpuDxe (the
#     control: it must fail naming exactly those two), and the images the
#     script installed, which must pass.
# A check whose fwtool, cache or image is absent says SKIP and why, and the
# run ends saying how many were skipped; AXLVMPOOL_REQUIRE_FIRMWARE=1 makes
# a SKIP a failure.
#
#   tools/firmware-tests.sh       FWTOOL=<path> for the real checks
set -euo pipefail
cd "$(dirname "$0")/.."
script=$PWD/guest/build-firmware.sh
tag=edk2-stable202602
commit=b7a715f7c03c45c6b4575bf88596bfd79658b8ce
tmp=$(mktemp -d)
copy=""
trap 'rm -rf "$tmp"; [[ -z "$copy" ]] || rm -rf "$copy"' EXIT
failed=0
skipped=0
fail() {
    echo "FAIL: $*" >&2
    failed=1
}
skip() {
    echo "SKIP $*" >&2
    skipped=$((skipped + 1))
}

# expect <name> <status> <stdout> <stderr> -- <command...>: exact matches.
expect() {
    local name=$1 want_rc=$2 want_out=$3 want_err=$4 rc=0 was=$failed
    failed=0
    shift 5
    "$@" >"$tmp/out" 2>"$tmp/err" || rc=$?
    [[ $rc == "$want_rc" ]] || fail "$name: exit $rc, wanted $want_rc"
    [[ "$(cat "$tmp/out")" == "$want_out" ]] ||
        fail "$name: stdout:"$'\n'"$(cat "$tmp/out")"$'\n'"wanted:"$'\n'"$want_out"
    [[ "$(cat "$tmp/err")" == "$want_err" ]] ||
        fail "$name: stderr:"$'\n'"$(cat "$tmp/err")"$'\n'"wanted:"$'\n'"$want_err"
    [[ $failed == 1 ]] || echo "ok: $name"
    [[ $was == 0 ]] || failed=1
}

# A fake fwtool, saying what axl-sdk's does: `find <image> <guid>` prints a
# node and exits 0, or, when <guid> is in FAKE_MISSING, says it is not found
# and exits 1. FAKE_FAIL fails as for a file with no firmware volume (exit 1
# too); FAKE_SILENT exits 0 printing nothing. Each GUID asked for is
# appended to FAKE_ASKED.
cat >"$tmp/fwtool" <<'EOF'
#!/usr/bin/env bash
[[ $1 == find && $# == 3 ]] || { echo "fake fwtool: bad call: $*" >&2; exit 64; }
[[ -z "${FAKE_ASKED:-}" ]] || echo "$3" >>"$FAKE_ASKED"
[[ -z "${FAKE_FAIL:-}" ]] || { echo "fwtool: '$2': no firmware volumes found" >&2; exit 1; }
[[ -z "${FAKE_SILENT:-}" ]] || exit 0
if [[ " ${FAKE_MISSING:-} " == *" $3 "* ]]; then
    echo "fwtool: GUID ${3,,} not found in '$2'" >&2
    exit 1
fi
echo "IMAGE[0x0]/VOLUME[0x0]/FILE[${3,,}] off=0x0 size=1"
EOF
chmod +x "$tmp/fwtool"
: >"$tmp/image.fd"
img=$tmp/image.fd
fs=7BD9DDF7-8B83-488E-AEC9-24C78610289C
gpu=D6099B94-CD97-4CC5-8714-7F6312701A8A

expect "every driver there" 0 "x64: $img holds every driver checked (11)" "" -- \
    env FWTOOL="$tmp/fwtool" FAKE_ASKED="$tmp/asked" "$script" --verify x64 "$img"
# The FILE_GUIDs of the drivers' INFs at the pinned tag, each asked once.
expect "the GUIDs asked for" 0 "$fs
$gpu
58E26F0D-CBAC-4BBA-B70F-18221415665A
A92CDB4B-82F1-4E0B-A516-8A655D371524
11D92DFB-3CA9-4F93-BA2E-4780ED3E03B5
FAB5D4F4-83C0-4AAF-8480-442D11DF6CEA
23CACE14-EBA4-49F6-9681-C697FF0B649E
5BEDB5CC-D830-4EB2-8742-2D4CC9B54F2C
2366C20F-E15A-11E3-8BF1-E4115B28BC50
3ACEB0C0-3C72-11E4-9A56-74D435052646
86CDDF93-4872-4597-8AF9-A35AE4D3725F" "" -- cat "$tmp/asked"
expect "two drivers missing, both named" 1 "" \
    "build-firmware.sh: aarch64: $img lacks: VirtioFsDxe ($fs) VirtioGpuDxe ($gpu)" -- \
    env FWTOOL="$tmp/fwtool" FAKE_MISSING="$fs $gpu" "$script" --verify aarch64 "$img"
expect "fwtool failing" 1 "" \
    "build-firmware.sh: x64: fwtool failed on $img: fwtool: '$img': no firmware volumes found" -- \
    env FWTOOL="$tmp/fwtool" FAKE_FAIL=1 "$script" --verify x64 "$img"
expect "fwtool naming no node" 1 "" \
    "build-firmware.sh: x64: fwtool found $fs in $img but named no node" -- \
    env FWTOOL="$tmp/fwtool" FAKE_SILENT=1 "$script" --verify x64 "$img"
expect "no fwtool" 1 "" \
    "build-firmware.sh: fwtool not found: set FWTOOL=<path> (axl-sdk builds it as fwtool-host)" -- \
    env FWTOOL="$tmp/none" "$script" --verify x64 "$img"
expect "no image" 1 "" "build-firmware.sh: x64: no image at $tmp/none.fd" -- \
    env FWTOOL="$tmp/fwtool" "$script" --verify x64 "$tmp/none.fd"
expect "an unknown arch" 1 "" "build-firmware.sh: --verify: arch is x64 or aarch64, not 'X64'" -- \
    env FWTOOL="$tmp/fwtool" "$script" --verify X64 "$img"
usage="usage: build-firmware.sh [--install] | --verify <x64|aarch64> <code-image> | --check-source <dir>"
expect "an unknown option" 2 "" "$usage" -- "$script" --instal
expect "too many arguments" 2 "" "$usage" -- "$script" --install x

# The source check, on a copy of the cache (cloned by reflink where the
# filesystem can, beside the cache so that it can).
expect "no checkout" 1 "" "build-firmware.sh: $tmp: not a git checkout" -- \
    "$script" --check-source "$tmp"
cache=${XDG_CACHE_HOME:-}
[[ "$cache" == /* ]] || cache="$HOME/.cache"
cache="$cache/axlvmpool/edk2"
if [[ ! -d "$cache/$tag/.git" ]]; then
    skip "the source check: no edk2 cache at $cache/$tag (guest/build-firmware.sh fetches it)"
else
    copy=$(mktemp -d "$cache/.test.XXXXXX")
    c=$copy/edk2
    # Under the build's lock, so that no build is changing the tree; its
    # build output left out.
    mkdir "$c"
    (
        if ! flock -n 9; then
            echo "tools/firmware-tests.sh: a build holds $cache/.$tag.lock; waiting" >&2
            flock 9
        fi
        tar -C "$cache/$tag" --exclude=./Build -cf - . | tar -C "$c" -xf -
    ) 9>"$cache/.$tag.lock"
    expect "the cache as fetched" 0 "$c: $tag at $commit, as committed" "" -- \
        "$script" --check-source "$c"
    echo changed >>"$c/License.txt"
    expect "a tracked file changed" 1 "" "build-firmware.sh: $c: tracked files changed:
 M License.txt" -- "$script" --check-source "$c"
    git -C "$c" checkout --quiet -- License.txt
    git -C "$c" update-index --skip-worktree License.txt
    expect "a file hidden from git status" 1 "" "build-firmware.sh: $c: files hidden from git status:
S License.txt" -- "$script" --check-source "$c"
    git -C "$c" update-index --no-skip-worktree License.txt
    fdt=$c/MdePkg/Library/BaseFdtLib/libfdt
    git -C "$fdt" update-index --skip-worktree README
    expect "a submodule's file hidden from git status" 1 "" \
        "build-firmware.sh: $c: files hidden from git status:
MdePkg/Library/BaseFdtLib/libfdt: S README" -- "$script" --check-source "$c"
    git -C "$fdt" update-index --no-skip-worktree README
    git -C "$c" submodule --quiet deinit -f MdePkg/Library/BaseFdtLib/libfdt
    sub=$(git -C "$c" ls-tree HEAD MdePkg/Library/BaseFdtLib/libfdt | awk '{ print $3 }')
    expect "a submodule not checked out" 1 "" \
        "build-firmware.sh: $c: submodules not at their recorded commits:
-$sub MdePkg/Library/BaseFdtLib/libfdt" -- "$script" --check-source "$c"
    git -C "$c" submodule --quiet update --init MdePkg/Library/BaseFdtLib/libfdt
    parent=$(git -C "$c" rev-parse HEAD~1)
    git -C "$c" tag -f "$tag" "$parent" >/dev/null
    expect "the tag moved" 1 "" "build-firmware.sh: $c: HEAD $commit, $tag $parent; pinned $commit" -- \
        "$script" --check-source "$c"
    git -C "$c" tag -f "$tag" "$commit" >/dev/null
    expect "the copy put back" 0 "$c: $tag at $commit, as committed" "" -- \
        "$script" --check-source "$c"
fi

# Real images, with the real fwtool; the install the script made.
data=${XDG_DATA_HOME:-}
[[ "$data" == /* ]] || data="$HOME/.local/share"
installed=$data/axlvmpool/firmware/$tag
real=${FWTOOL:-$(command -v fwtool || true)}
if [[ -z "$real" ]]; then
    skip "real images: no fwtool (set FWTOOL=<path>)"
else
    distro=/usr/share/edk2/ovmf/OVMF_CODE.fd
    if [[ -f $distro ]]; then
        expect "the distro's OVMF lacks VirtioFsDxe and VirtioGpuDxe" 1 "" \
            "build-firmware.sh: x64: $distro lacks: VirtioFsDxe ($fs) VirtioGpuDxe ($gpu)" -- \
            env FWTOOL="$real" "$script" --verify x64 "$distro"
    else
        skip "the distro control: no $distro"
    fi
    if [[ -f $installed/manifest.json ]]; then
        for arch in x64 aarch64; do
            expect "the installed $arch image" 0 \
                "$arch: $installed/$arch/code.fd holds every driver checked (11)" "" -- \
                env FWTOOL="$real" "$script" --verify "$arch" "$installed/$arch/code.fd"
        done
    else
        skip "the installed images: none in $installed"
    fi
fi

# The installed descriptors: each parses, equals exactly what it should
# say, names files that exist, and the manifest lists both.
if [[ -f $installed/manifest.json ]]; then
    expect "the installed descriptors" 0 "" "" -- python3 -I - "$data/axlvmpool/firmware" "$tag" \
        "$commit" <<'EOF'
import json
import pathlib
import sys

root, tag, commit = sys.argv[1:]
d = f"{root}/descriptors"
manifest = json.loads(pathlib.Path(f"{root}/{tag}/manifest.json").read_text())
names = {"x64": ("x86_64", ["pc-q35-*"], ["acpi-s3", "amd-sev", "amd-sev-es", "verbose-dynamic"],
                 "X64 (q35)"),
         "aarch64": ("aarch64", ["virt-*"], ["verbose-static"], "AARCH64 (virt)")}
paths = [f"{d}/10-axlvmpool-{tag}-{a}.json" for a in names]
assert manifest["descriptors"] == paths, manifest["descriptors"]
for (a, (qarch, machines, features, what)), path in zip(names.items(), paths):
    got = json.loads(pathlib.Path(path).read_text())
    want = {
        "description": f"axlvmpool's own UEFI firmware for {what}, edk2 {tag}",
        "interface-types": ["uefi"],
        "mapping": {
            "device": "flash",
            "mode": "split",
            "executable": {"filename": f"{root}/{tag}/{a}/code.fd", "format": "raw"},
            "nvram-template": {"filename": f"{root}/{tag}/{a}/vars.fd", "format": "raw"},
        },
        "targets": [{"architecture": qarch, "machines": machines}],
        "features": features,
        "tags": [tag, commit],
    }
    assert got == want, f"{path}: {got}"
    for f in ("executable", "nvram-template"):
        assert pathlib.Path(got["mapping"][f]["filename"]).is_file(), got["mapping"][f]
EOF
else
    skip "the installed descriptors: no install in $installed"
fi
[[ $failed == 0 ]] || exit 1
if ((skipped > 0)); then
    echo "tools/firmware-tests.sh: passed, $skipped skipped"
    [[ "${AXLVMPOOL_REQUIRE_FIRMWARE:-}" != 1 ]] || {
        echo "tools/firmware-tests.sh: AXLVMPOOL_REQUIRE_FIRMWARE=1: a SKIP fails" >&2
        exit 1
    }
else
    echo "tools/firmware-tests.sh: all passed"
fi
