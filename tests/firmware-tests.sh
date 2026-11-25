#!/usr/bin/env bash
# The checks of build.sh that need no build and no QEMU:
#   - the unit tests of lib/axlfw (tests/test_axlfw.py): the FV walker on
#     stand-in images, the Shell's PE checks, the AutoGen facts, the
#     descriptors exactly, the Shell output parsers, and the install from
#     local tarballs (digests, a corrupt set, an escaping member, upgrades);
#   - build.sh's own arguments and `verify` errors, exactly; two runs making
#     the venv at once (one makes it); a failed build leaving no earlier
#     release in dist/;
#   - `check-source` against a copy of an edk2 source at the pinned tag (by
#     default build.sh's cache; AXL_FIRMWARE_EDK2=<dir> for another): the
#     copy as it is passes, and a tracked file changed, an untracked file,
#     a file hidden from git status (in the tree and in a submodule), a
#     submodule not checked out, the tag moved, and a directory that is no
#     checkout each fail, said, as does an untracked file hidden by
#     .git/info/exclude or the user's core.excludesFile (in the tree and in a
#     submodule); a file the tree's .gitignore ignores (a build's output)
#     passes;
#   - each checked GUID against its INF in that source (`source-guids`);
#   - real images: the distribution's OVMF (Fedora/EL paths) as controls,
#     which must fail exactly (no VirtioFsDxe, no pointer drivers; the
#     Secure Boot image holding SecureBootConfigDxe), and an installed
#     release's images, which must pass.
# A check whose input is absent says SKIP and why; the run ends saying how
# many were skipped. AXL_FIRMWARE_REQUIRE makes a SKIP a failure: 1 for
# every one, or the names of those that must run, of "source" (the source
# checks), "controls" (the distribution's images, on Fedora or EL) and
# "installed" (an installed release); CI's build job requires "source
# installed".
set -euo pipefail
cd "$(dirname "$0")/.."
here=$PWD
script=$here/build.sh
tag=edk2-stable202611
commit=556dcfb466e9f05affeaff881d738807f8156d3c
tmp=$(mktemp -d)
copy=""
trap 'rm -rf "$tmp"; [[ -z "$copy" ]] || { chmod -R u+w "$copy"; rm -rf "$copy"; }' EXIT
failed=0
skipped=0
fail() {
    echo "FAIL: $*" >&2
    failed=1
}
for name in ${AXL_FIRMWARE_REQUIRE:-}; do
    case "$name" in
        1 | source | controls | installed) ;;
        *)
            echo "tests/firmware-tests.sh: AXL_FIRMWARE_REQUIRE: no check '$name' (1, source, controls, installed)" >&2
            exit 2
            ;;
    esac
done

# skip <name> <why>: the check <name> did not run.
skip() {
    local name=$1
    shift
    if [[ " ${AXL_FIRMWARE_REQUIRE:-} " == *" $name "* ]]; then
        fail "$*: AXL_FIRMWARE_REQUIRE requires $name"
        return
    fi
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

cache=${XDG_CACHE_HOME:-}
[[ "$cache" == /* ]] || cache="$HOME/.cache"
# The venv build.sh uses, named by requirements.txt's digest (it makes it
# on first use).
"$script" verify x64 "$here/tests/fixtures/volumes/both.fd" >/dev/null 2>&1 || true
py=$cache/axl-firmware/venv-$(sha256sum requirements.txt | cut -c1-16)/bin/python
[[ -x "$py" ]] || {
    echo "tests/firmware-tests.sh: no venv from build.sh" >&2
    exit 1
}
if "$py" -E -s -m unittest discover -s tests -p 'test_*.py' >"$tmp/unit" 2>&1; then
    echo "ok: unit tests ($(grep -o 'Ran [0-9]* tests' "$tmp/unit"))"
else
    cat "$tmp/unit" >&2
    fail "unit tests"
fi

usage="usage: build.sh build [--edk2 DIR] [--install] [--prefix DIR]
       build.sh install --from <tarball|release-tag> [--prefix DIR] [--sha256 HEX] [--symbols]
       build.sh check <release-dir>
       build.sh verify <x64|aarch64> <code-image>
       build.sh check-source <dir>
       build.sh fetch"
expect "no command" 2 "" "$usage" -- "$script"
expect "an unknown command" 2 "" "$usage" -- "$script" make
expect "an unknown build option" 2 "" "$usage" -- "$script" build --instal
expect "verify, too many arguments" 2 "" "$usage" -- "$script" verify x64 a b
expect "verify, an unknown arch" 1 "" "build.sh: verify: arch is x64 or aarch64, not 'X64'" -- \
    "$script" verify X64 "$tmp/none.fd"
expect "verify, no image" 1 "" "build.sh: x64: no image at $tmp/none.fd" -- \
    "$script" verify x64 "$tmp/none.fd"
nv=$here/tests/fixtures/volumes/not-a-volume.fd
expect "verify, not a volume" 1 "" "build.sh: x64: $nv: no firmware volume found" -- \
    "$script" verify x64 "$nv"
expect "check-source, no checkout" 1 "" "build.sh: $tmp: not a git checkout" -- \
    "$script" check-source "$tmp"

# Two runs making the venv at once: one makes it, the other waits for it. A
# stand-in python3 counts the venvs made; its venv's pip takes a second.
fake=$tmp/fake-python
mkdir -p "$fake"
cat >"$fake/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "$1 $2" == "-m venv" ]]; then
    echo venv >>"$AXLFW_TEST_COUNT"
    mkdir -p "$3/bin"
    printf '#!/usr/bin/env bash\n[[ $1 != -m ]] || sleep 1\n' >"$3/bin/python"
    chmod +x "$3/bin/python"
fi
EOF
chmod +x "$fake/python3"
race=(env XDG_CACHE_HOME="$tmp/race-cache" PATH="$fake:$PATH" AXLFW_TEST_COUNT="$tmp/venvs"
    "$script" verify x64 "$here/tests/fixtures/volumes/both.fd")
"${race[@]}" >"$tmp/race1" 2>&1 &
first=$!
"${race[@]}" >"$tmp/race2" 2>&1 || fail "the venv race: the second run failed: $(cat "$tmp/race2")"
wait "$first" || fail "the venv race: the first run failed: $(cat "$tmp/race1")"
if [[ "$(cat "$tmp/venvs")" == venv ]]; then
    echo "ok: two runs make one venv"
else
    fail "the venv race: venvs made: $(wc -l <"$tmp/venvs")"
fi

# A build that fails leaves no earlier release in dist/.
stale=$tmp/stale
mkdir -p "$stale/dist"
cp "$script" "$stale/build.sh"
touch "$stale/dist/axl-firmware-edk2-stable202611-r1.tar.xz" "$stale/dist/SHA256SUMS" "$stale/dist/notes.txt"
if GCC_AARCH64_PREFIX=/nonexistent/ "$stale/build.sh" build >"$tmp/out" 2>&1; then
    fail "a failed build: it did not fail: $(cat "$tmp/out")"
elif [[ "$(find "$stale/dist" -mindepth 1 -printf '%f ')" != "notes.txt " ]]; then
    fail "a failed build left in dist/: $(find "$stale/dist" -mindepth 1 -printf '%f ')"
else
    echo "ok: a failed build leaves no earlier release in dist/"
fi

# The source checks, on a copy (the cache is read-only).
src=${AXL_FIRMWARE_EDK2:-$cache/axl-firmware/edk2/$tag}
if [[ ! -e "$src/.git" ]]; then
    skip source "the source checks: no edk2 source at $src (build.sh build fetches it; or AXL_FIRMWARE_EDK2=<dir>)"
else
    expect "each GUID is its INF's" 0 "$src: every checked GUID is its INF's (or DEC's)" "" -- \
        "$py" -E -s "$here/lib/axlfw-run.py" source-guids "$src"
    copy=$(mktemp -d "$tmp.src.XXXXXX")
    c=$copy/edk2
    mkdir "$c"
    tar -C "$src" --exclude=./Build -cf - . | tar -C "$c" -xf -
    chmod -R u+w "$c"
    expect "the source as it is" 0 "$c: $tag at $commit, as committed" "" -- "$script" check-source "$c"
    echo changed >>"$c/License.txt"
    expect "a tracked file changed" 1 "" "build.sh: $c: files changed or untracked:
 M License.txt" -- "$script" check-source "$c"
    git -C "$c" checkout --quiet -- License.txt
    echo new >"$c/OvmfPkg/stray.inc"
    expect "an untracked file" 1 "" "build.sh: $c: files changed or untracked:
?? OvmfPkg/stray.inc" -- "$script" check-source "$c"
    # Hidden by the user's own ignore rules, still untracked.
    echo 'stray.inc' >>"$c/.git/info/exclude"
    expect "an untracked file .git/info/exclude hides" 1 "" "build.sh: $c: files changed or untracked:
?? OvmfPkg/stray.inc" -- "$script" check-source "$c"
    sed -i.bak '$d' "$c/.git/info/exclude" && rm "$c/.git/info/exclude.bak"
    printf '[core]\n\texcludesFile = %s\n' "$tmp/user-ignore" >"$tmp/user-gitconfig"
    echo '*.inc' >"$tmp/user-ignore"
    expect "an untracked file core.excludesFile hides" 1 "" "build.sh: $c: files changed or untracked:
?? OvmfPkg/stray.inc" -- env GIT_CONFIG_GLOBAL="$tmp/user-gitconfig" "$script" check-source "$c"
    rm "$c/OvmfPkg/stray.inc"
    fdt=$c/MdePkg/Library/BaseFdtLib/libfdt
    echo new >"$fdt/stray.inc"
    expect "a submodule's untracked file core.excludesFile hides" 1 "" \
        "build.sh: $c: files changed or untracked:
?? MdePkg/Library/BaseFdtLib/libfdt/stray.inc" -- env GIT_CONFIG_GLOBAL="$tmp/user-gitconfig" "$script" check-source "$c"
    rm "$fdt/stray.inc"
    mkdir -p "$c/Build/x"
    echo output >"$c/Build/x/out.efi"
    expect "an ignored build output" 0 "$c: $tag at $commit, as committed" "" -- "$script" check-source "$c"
    rm -rf "$c/Build"
    git -C "$c" update-index --skip-worktree License.txt
    expect "a file hidden from git status" 1 "" "build.sh: $c: files hidden from git status:
S License.txt" -- "$script" check-source "$c"
    git -C "$c" update-index --no-skip-worktree License.txt
    git -C "$fdt" update-index --skip-worktree README
    expect "a submodule's file hidden from git status" 1 "" \
        "build.sh: $c: files hidden from git status:
MdePkg/Library/BaseFdtLib/libfdt: S README" -- "$script" check-source "$c"
    git -C "$fdt" update-index --no-skip-worktree README
    git -C "$c" submodule --quiet deinit -f MdePkg/Library/BaseFdtLib/libfdt
    sub=$(git -C "$c" ls-tree HEAD MdePkg/Library/BaseFdtLib/libfdt | awk '{ print $3 }')
    expect "a submodule not checked out" 1 "" \
        "build.sh: $c: submodules not at their recorded commits:
-$sub MdePkg/Library/BaseFdtLib/libfdt" -- "$script" check-source "$c"
    git -C "$c" submodule --quiet update --init MdePkg/Library/BaseFdtLib/libfdt
    parent=$(git -C "$c" rev-parse HEAD~1)
    git -C "$c" tag -f "$tag" "$parent" >/dev/null
    expect "the tag moved" 1 "" "build.sh: $c: HEAD $commit, $tag $parent; pinned $commit" -- \
        "$script" check-source "$c"
    git -C "$c" tag -f "$tag" "$commit" >/dev/null
    expect "the copy put back" 0 "$c: $tag at $commit, as committed" "" -- "$script" check-source "$c"
fi

# Real images. The distribution's are controls, judged only at the digest
# their expected text was taken from.
fs="VirtioFsDxe (7BD9DDF7-8B83-488E-AEC9-24C78610289C)"
gpu="VirtioGpuDxe (D6099B94-CD97-4CC5-8714-7F6312701A8A)"
mice="UsbMouseDxe (2D2E62AA-9ECF-43B7-8219-94E7FC713DFE), UsbMouseAbsolutePointerDxe (4EA43463-747C-46EB-97FB-B0E5C5F05306)"
ps2="Ps2MouseDxe (08464531-4C99-4C4C-A887-8D8BA4BBB063)"
control() {
    local name=$1 image=$2 digest=$3 want=$4
    if [[ ! -f "$image" ]]; then
        skip controls "$name: no $image"
    elif [[ "$(sha256sum <"$image" | cut -d' ' -f1)" != "$digest" ]]; then
        skip controls "$name: $image is not the build the expected text was taken from ($digest)"
    else
        expect "$name" 1 "" "$want" -- "$script" verify x64 "$image"
    fi
}
d=/usr/share/edk2/ovmf
control "the distribution's OVMF lacks VirtioFsDxe and the pointer drivers" "$d/OVMF_CODE.fd" \
    fab13b2abe7dcea71a6809f3368e02d2b18f76773bade14a98f98342507e84b6 \
    "build.sh: x64: $d/OVMF_CODE.fd lacks: $fs, $gpu, $mice, $ps2"
control "the distribution's Secure Boot OVMF holds SecureBootConfigDxe" "$d/OVMF_CODE.secboot.fd" \
    e6a787293c6f193eaff7feeb1f929871d1b27dd72f64ce66d9326bfa0604b249 \
    "build.sh: x64: $d/OVMF_CODE.secboot.fd lacks: $fs, $gpu, $mice, Shell (7C04A583-9E3E-4F1C-AD65-E05268D0B4D1), $ps2
build.sh: x64: $d/OVMF_CODE.secboot.fd holds: SecureBootConfigDxe (F0E6A44F-7195-41C3-AC64-54F202CD0A21)"

data=${XDG_DATA_HOME:-}
[[ "$data" == /* ]] || data="$HOME/.local/share"
installed=$(find "$data/axl-firmware" -mindepth 1 -maxdepth 1 -name "$tag-r*" 2>/dev/null | LC_ALL=C sort -V | tail -1 || true)
if [[ -n "$installed" && -f "$installed/manifest.json" ]]; then
    for dir in x64 aarch64 release/x64 release/aarch64; do
        arch=${dir#release/}
        n=$("$py" -E -s -c 'import sys; sys.path.insert(0, sys.argv[1]); from axlfw import checks; print(len(checks.drivers(sys.argv[2])))' "$here/lib" "$arch")
        expect "the installed $dir image" 0 \
            "$arch: $installed/$dir/code.fd holds every driver checked ($n) and none of 1 absent" "" -- \
            "$script" verify "$arch" "$installed/$dir/code.fd"
    done
else
    skip installed "the installed images: none in $data/axl-firmware"
fi

[[ $failed == 0 ]] || exit 1
if ((skipped > 0)); then
    echo "tests/firmware-tests.sh: passed, $skipped skipped"
    [[ " ${AXL_FIRMWARE_REQUIRE:-} " != *" 1 "* ]] || {
        echo "tests/firmware-tests.sh: AXL_FIRMWARE_REQUIRE=1: a SKIP fails" >&2
        exit 1
    }
else
    echo "tests/firmware-tests.sh: all passed"
fi
