#!/usr/bin/env bash
# Build the pool's own UEFI firmware (DESIGN.md §11 item 101): edk2 at a
# pinned stable tag, OVMF for X64 (q35, split CODE and VARS, raw) and
# ArmVirtQemu for AARCH64 (pflash CODE and VARS, raw, padded to 64 MiB),
# built with the options QEMU builds its bundled images with
# (roms/edk2-build.config, QEMU 10.0.0), into
#   guest/target/firmware/<tag>/{x64,aarch64}/{code.fd,vars.fd,Shell_<ARCH>.efi}
#   guest/target/firmware/<tag>/manifest.json
# and verify each image before anything is installed: the drivers below are
# in its firmware volumes (read with axl-sdk's fwtool), and the source holds
# edk2's VirtioRng fix (8ba0263, item 99's stall).
#
# --install: then put them where the pool's discovery prefers them:
#   ${XDG_DATA_HOME:-~/.local/share}/axlvmpool/firmware/<tag>/...
# each file written beside its place and renamed into it, the manifest last,
# so a reader that checks the manifest's digests never takes a half-installed
# set (each rename is atomic, the set is not). Then a QEMU firmware
# descriptor per arch naming them, which discovery searches first:
#   .../axlvmpool/firmware/descriptors/10-axlvmpool-<tag>-{x64,aarch64}.json
# (listed in the manifest, for a later uninstall).
#
# --verify <x64|aarch64> <code-image>: only check one image's drivers (no
# build); exit 0 when every driver is there, 1 naming each missing one.
# --check-source <dir>: only check an edk2 tree as the cache is checked
# (below); exit 0 when it passes, 1 saying why not.
#
# The edk2 source is fetched once into a cache,
#   ${XDG_CACHE_HOME:-~/.cache}/axlvmpool/edk2/<tag>
# (a blob-less clone of the tag, with the submodules the build needs), and
# reused while it checks out: HEAD and the tag are the pinned commit, the
# tracked files and submodules are as committed (none hidden from git
# status), and the fix is an ancestor. Each build starts from a clean tree
# (git clean, in the submodules too: no earlier Build/, Conf/ or BaseTools
# output is reused), so the images are built from the pinned source alone.
# EDK2_SRC=<clone> seeds that cache from an existing edk2 clone (a local
# clone of it, so the clone itself is never written to); objects it lacks
# are fetched from upstream.
#
# Needs git, python3 (3.9 or later), make, gcc, libuuid's headers
# (libuuid-devel or uuid-dev, for BaseTools), nasm, iasl,
# aarch64-linux-gnu-gcc (or GCC_AARCH64_PREFIX=<prefix>), flock, and
# fwtool (FWTOOL=<path>, or on PATH; axl-sdk builds it as fwtool-host).
# The build runs at nice 19 with JOBS (default 4) jobs.
set -euo pipefail

tag=edk2-stable202602
commit=b7a715f7c03c45c6b4575bf88596bfd79658b8ce
url=https://github.com/tianocore/edk2.git
# "OvmfPkg/VirtioRngDxe: protect VirtioRngGetRNG against timer callbacks"
# (it raises the TPL) -- the fix for item 99's stall.
fix=8ba02634ecaa6ff5d0edcea266198f42eca90c53
version="$tag-for-axlvmpool"

# The drivers each image must hold (FILE_GUIDs from their INFs), as
# "name guid" lines; both arches take the same list.
drivers="VirtioFsDxe 7BD9DDF7-8B83-488E-AEC9-24C78610289C
VirtioGpuDxe D6099B94-CD97-4CC5-8714-7F6312701A8A
VirtioRngDxe 58E26F0D-CBAC-4BBA-B70F-18221415665A
VirtioNetDxe A92CDB4B-82F1-4E0B-A516-8A655D371524
VirtioBlkDxe 11D92DFB-3CA9-4F93-BA2E-4780ED3E03B5
VirtioScsiDxe FAB5D4F4-83C0-4AAF-8480-442D11DF6CEA
VirtioSerialDxe 23CACE14-EBA4-49F6-9681-C697FF0B649E
Ip6Dxe 5BEDB5CC-D830-4EB2-8742-2D4CC9B54F2C
HttpDxe 2366C20F-E15A-11E3-8BF1-E4115B28BC50
TlsDxe 3ACEB0C0-3C72-11E4-9A56-74D435052646
IScsiDxe 86CDDF93-4872-4597-8AF9-A35AE4D3725F"

die() {
    echo "build-firmware.sh: $*" >&2
    exit 1
}

# find_fwtool: set fwtool to FWTOOL, or fwtool on PATH, or die.
find_fwtool() {
    fwtool=${FWTOOL:-$(command -v fwtool || true)}
    [[ -n "$fwtool" && -x "$fwtool" ]] ||
        die "fwtool not found: set FWTOOL=<path> (axl-sdk builds it as fwtool-host)"
}

# verify_image <arch> <image>: every driver above is in <image>.
verify_image() {
    local arch=$1 image=$2 name guid out missing=""
    [[ -f "$image" ]] || die "$arch: no image at $image"
    find_fwtool
    while read -r name guid; do
        # fwtool prints each node holding <guid> and exits 0; when none does
        # it says "GUID <guid> not found in" and exits 1, as for any error.
        if out=$("$fwtool" find "$image" "$guid" 2>&1); then
            [[ -n "$out" ]] || die "$arch: fwtool found $guid in $image but named no node"
        elif [[ "$out" == "fwtool: GUID ${guid,,} not found in '$image'" ]]; then
            missing+=" $name ($guid)"
        else
            die "$arch: fwtool failed on $image: $out"
        fi
    done <<<"$drivers"
    if [[ -n "$missing" ]]; then
        echo "build-firmware.sh: $arch: $image lacks:$missing" >&2
        return 1
    fi
    echo "$arch: $image holds every driver checked ($(wc -l <<<"$drivers"))"
}

# The submodules the build needs, in the tree at <dir>: all but the
# unit-test frameworks'.
submodules() {
    git -C "$1" config -f .gitmodules --get-regexp '^submodule\..*\.path$' |
        awk '$2 !~ /^UnitTestFrameworkPkg\// { print $2 }'
}

# source_ok <dir>: the tree is the pinned commit, as committed, with the fix.
source_ok() {
    local d=$1 head tagged dirty hidden sub
    [[ -d "$d/.git" ]] || {
        echo "build-firmware.sh: $d: not a git checkout" >&2
        return 1
    }
    head=$(git -C "$d" rev-parse HEAD 2>/dev/null) || return 1
    tagged=$(git -C "$d" rev-parse "refs/tags/$tag^{commit}" 2>/dev/null) || tagged=none
    if [[ "$head" != "$commit" || "$tagged" != "$commit" ]]; then
        echo "build-firmware.sh: $d: HEAD $head, $tag $tagged; pinned $commit" >&2
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
        echo "build-firmware.sh: $d: files hidden from git status:" >&2
        head -5 <<<"$hidden" >&2
        return 1
    fi
    dirty=$(git -C "$d" status --porcelain --untracked-files=no --ignore-submodules=none) || return 1
    if [[ -n "$dirty" ]]; then
        echo "build-firmware.sh: $d: tracked files changed:" >&2
        echo "$dirty" >&2
        return 1
    fi
    # A leading ' ' is a submodule checked out at its recorded commit.
    sub=$(submodules "$d" | (cd "$d" && xargs git submodule status --)) || return 1
    if grep -v '^ ' <<<"$sub" | grep -q .; then
        echo "build-firmware.sh: $d: submodules not at their recorded commits:" >&2
        grep -v '^ ' <<<"$sub" >&2
        return 1
    fi
    # With HEAD the pinned commit this holds whenever the two pins agree: it
    # guards the pins themselves (a tag moved without its fix).
    if ! git -C "$d" merge-base --is-ancestor "$fix" HEAD; then
        echo "build-firmware.sh: $d: lacks the VirtioRng fix $fix" >&2
        return 1
    fi
}

install=0
case "$#:${1:-}" in
    0:) ;;
    1:--install) install=1 ;;
    3:--verify)
        case "$2" in
            x64 | aarch64) ;;
            *) die "--verify: arch is x64 or aarch64, not '$2'" ;;
        esac
        verify_image "$2" "$3"
        exit
        ;;
    2:--check-source)
        source_ok "$2" || exit 1
        echo "$2: $tag at $commit, as committed"
        exit
        ;;
    *)
        echo "usage: build-firmware.sh [--install] | --verify <x64|aarch64> <code-image>" \
            "| --check-source <dir>" >&2
        exit 2
        ;;
esac

here=$(cd "$(dirname "$0")" && pwd)
jobs=${JOBS:-4}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || die "JOBS must be a positive number, not '$jobs'"
aa64_prefix=${GCC_AARCH64_PREFIX:-aarch64-linux-gnu-}
aa64_gcc="${aa64_prefix}gcc"
for tool in git python3 make gcc nasm iasl flock truncate sha256sum "$aa64_gcc"; do
    command -v "$tool" >/dev/null || die "$tool not found on PATH"
done
find_fwtool
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' ||
    die "python3 is $(python3 -V 2>&1); 3.9 or later is needed"
printf '#include <uuid/uuid.h>\n' | gcc -E - >/dev/null 2>&1 ||
    die "libuuid's headers (uuid/uuid.h) not found: install libuuid-devel or uuid-dev"
# XDG paths that are not absolute are to be ignored (the XDG spec).
cache=${XDG_CACHE_HOME:-}
[[ "$cache" == /* ]] || cache="$HOME/.cache"
cache="$cache/axlvmpool/edk2"
src="$cache/$tag"
mkdir -p "$cache"
# One run at a time per cache: a second waits rather than share the tree.
exec 9>"$cache/.$tag.lock"
if ! flock -n 9; then
    echo "build-firmware.sh: another run holds $cache/.$tag.lock; waiting" >&2
    flock 9
fi

fetch_tmp=""
trap '[[ -z "$fetch_tmp" ]] || rm -rf "$fetch_tmp"' EXIT

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
    rm -rf "$src"
    mv "$t" "$src"
    rm -rf "$fetch_tmp"
    fetch_tmp=""
}

if source_ok "$src"; then
    echo "+ reusing the edk2 cache $src"
else
    [[ ! -e "$src" ]] || echo "build-firmware.sh: the edk2 cache $src does not check out; fetching again" >&2
    fetch_source
fi
# Nothing untracked survives into the build: not an earlier build's
# output, nor a file dropped into a package.
git -C "$src" clean -ffdxq
git -C "$src" submodule foreach --quiet git clean -ffdxq

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
# 10.0.0 bundles (DESIGN.md §11 item 101):
#  - the Boot Manager Menu is UiApp (462CAA21-7614-4503-836E-8AB6F4662331),
#    the front page. edk2 changed it to BootManagerMenuApp for OVMF
#    (9d9e3a2b) and ArmVirtPkg (4e19daa8), so a boot option that returns
#    EFI_SUCCESS (the Shell's `exit`) landed in the boot menu instead;
#  - the front page's text mode 80x25 and video mode 640x480, stable202408's
#    (the dec's text default; the DSCs' video). OvmfDisplayPcds.dsc.inc,
#    which both DSCs include since, sets setup's text mode to 0x0 (the
#    largest; 240x56 on a serial console with TerminalDxe's new modes) and
#    its video to 1280x800.
common_pcds=(
    "PcdBootManagerMenuFile=H{0x21,0xaa,0x2c,0x46,0x14,0x76,0x03,0x45,0x83,0x6e,0x8a,0xb6,0xf4,0x66,0x23,0x31}"
    PcdSetupConOutColumn=80
    PcdSetupConOutRow=25
    PcdSetupVideoHorizontalResolution=640
    PcdSetupVideoVerticalResolution=480
)
# edk2-build.py's defaults: no tool or tgts in the config.
toolchain=GCC5
target=DEBUG
# edk2-build.py's --version-override and --release-date, as QEMU's
# roms/Makefile passes them: the tag (ours suffixed) and its commit's date.
release_date=$(git -C "$src" log -1 --pretty=format:%cd --date=format:%m/%d/%Y "$commit")

# edk2_build <dsc> <arch> <opts...> -- <pcds...>
edk2_build() {
    local dsc=$1 arch=$2 a
    shift 2
    local -a cmd=(build -t "$toolchain" -b "$target" -p "$dsc" -a "$arch" -n "$jobs"
        --pcd "PcdFirmwareVersionString=L$version\\0"
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
    nice -n 19 "${cmd[@]}"
}

(
    cd "$src"
    export WORKSPACE="$src" PACKAGES_PATH="$src" EDK_TOOLS_PATH="$src/BaseTools"
    export CONF_PATH="$src/Conf" PYTHON_COMMAND=python3 PYTHONHASHSEED=1
    export GCC5_AARCH64_PREFIX="$aa64_prefix" GCC_AARCH64_PREFIX="$aa64_prefix"
    echo "+ make -C BaseTools"
    nice -n 19 make -C BaseTools -j "$jobs" >/dev/null
    # edksetup.sh is not written for -e or -u; whether it worked is
    # whether `build` is on PATH after it.
    set +eu
    # Sourced, it would read this script's arguments (--install) as its own.
    set --
    # shellcheck disable=SC1091
    . ./edksetup.sh >/dev/null
    set -eu
    [[ "$(command -v build)" == "$src/BaseTools/BinWrappers/PosixLike/build" ]] ||
        die "edksetup.sh did not put this tree's BaseTools build first on PATH" \
            "(found: $(command -v build || echo none))"
    edk2_build OvmfPkg/OvmfPkgX64.dsc X64 "${common_opts[@]}" -- "${common_pcds[@]}"
    edk2_build ArmVirtPkg/ArmVirtQemu.dsc AARCH64 "${common_opts[@]}" "${aa64_opts[@]}" \
        -- "${common_pcds[@]}" "${aa64_pcds[@]}"
)

out="$here/target/firmware/$tag"
rm -rf "$out"
mkdir -p "$out/x64" "$out/aarch64"
x64="$src/Build/OvmfX64/${target}_$toolchain"
# The DSCs' OUTPUT_DIRECTORY at the pinned tag (QEMU's edk2-build.config
# still says ArmVirtQemu-AARCH64, the name before edk2 renamed it).
aa64="$src/Build/ArmVirtQemu-AArch64/${target}_$toolchain"
cp "$x64/FV/OVMF_CODE.fd" "$out/x64/code.fd"
cp "$x64/FV/OVMF_VARS.fd" "$out/x64/vars.fd"
cp "$x64/X64/Shell.efi" "$out/x64/Shell_X64.efi"
cp "$aa64/FV/QEMU_EFI.fd" "$out/aarch64/code.fd"
cp "$aa64/FV/QEMU_VARS.fd" "$out/aarch64/vars.fd"
cp "$aa64/AARCH64/Shell.efi" "$out/aarch64/Shell_AARCH64.efi"
# QEMU's pad1/pad2: a virt machine's pflash banks are 64 MiB each. An image
# larger than that is refused, not cut.
for f in "$out/aarch64/code.fd" "$out/aarch64/vars.fd"; do
    (($(stat -c %s "$f") <= 67108864)) || die "$f is larger than its 64 MiB flash bank"
done
truncate --size 64M "$out/aarch64/code.fd" "$out/aarch64/vars.fd"

ok=1
verify_image x64 "$out/x64/code.fd" || ok=0
verify_image aarch64 "$out/aarch64/code.fd" || ok=0
[[ $ok == 1 ]] || die "the built images do not verify; nothing installed"

# Where --install puts them: the versioned set, and QEMU firmware
# descriptors (docs/interop/firmware.json) naming it, in a directory of the
# pool's own -- not ~/.config/qemu/firmware, which libvirt reads too and
# would switch the user's own VMs to these images.
dest="" descriptors=""
if [[ $install == 1 ]]; then
    data=${XDG_DATA_HOME:-}
    [[ "$data" == /* ]] || data="$HOME/.local/share"
    dest="$data/axlvmpool/firmware/$tag"
    descriptors="$data/axlvmpool/firmware/descriptors"
fi

# The manifest: what was built, from what, how, what was verified, each
# file's digest, and the descriptors an install writes (so that a later
# uninstall can find them). Written only once the images verified. With
# --install, the descriptors too, staged in $out/descriptors.
python3 -I - "$out" "$tag" "$commit" "$url" "$fix" "$version" "$release_date" \
    "$toolchain" "$target" "gcc $(gcc -dumpfullversion)" \
    "$aa64_gcc $("$aa64_gcc" -dumpfullversion)" "$drivers" \
    "${common_opts[*]}" "${aa64_opts[*]}" "${common_pcds[*]} ${aa64_pcds[*]}" "${common_pcds[*]}" \
    "$dest" "$descriptors" <<'EOF'
import datetime
import hashlib
import json
import pathlib
import sys

(out, tag, commit, url, fix, version, release_date, toolchain, target, gcc,
 aa64_gcc, drivers, common, aa64_opts, aa64_pcds, x64_pcds, dest, descriptors) = sys.argv[1:]
root = pathlib.Path(out)


def entry(rel: str) -> dict[str, object]:
    data = (root / rel).read_bytes()
    return {"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


checked = [{"name": n, "guid": g} for n, g in (line.split() for line in drivers.splitlines())]
pcds = [f"PcdFirmwareVersionString=L{version}\\0",
        f"PcdFirmwareReleaseDateString=L{release_date}\\0"]


def arch(name: str, machine: str, platform: str, defines: list[str], pcds: list[str],
         shell: str) -> dict[str, object]:
    return {
        "machine": machine,
        "platform": platform,
        "defines": defines,
        "pcds": pcds,
        "code": entry(f"{name}/code.fd"),
        "vars": entry(f"{name}/vars.fd"),
        "shell": entry(f"{name}/{shell}"),
        "verified": {"by": "fwtool find", "drivers": checked},
    }


# The descriptors' features, as QEMU's own descriptors label its images
# built with these flags: X64 is DEBUG with OVMF's debug port
# (verbose-dynamic), S3 and SEV/SEV-ES; AARCH64 is DEBUG with its output on
# the serial port by static configuration (verbose-static), errors only
# at DEBUG_PRINT_ERROR_LEVEL 0x80000000 but static serial output all the
# same (the coordinator's call, DESIGN.md §11 item 101).
targets = {
    "x64": ("x86_64", ["pc-q35-*"], ["acpi-s3", "amd-sev", "amd-sev-es", "verbose-dynamic"],
            "X64 (q35)"),
    "aarch64": ("aarch64", ["virt-*"], ["verbose-static"], "AARCH64 (virt)"),
}
written: list[str] = []
if dest:
    (root / "descriptors").mkdir()
    for name, (qarch, machines, features, what) in targets.items():
        base = f"10-axlvmpool-{tag}-{name}.json"
        descriptor = {
            "description": f"axlvmpool's own UEFI firmware for {what}, edk2 {tag}",
            "interface-types": ["uefi"],
            "mapping": {
                "device": "flash",
                "mode": "split",
                "executable": {"filename": f"{dest}/{name}/code.fd", "format": "raw"},
                "nvram-template": {"filename": f"{dest}/{name}/vars.fd", "format": "raw"},
            },
            "targets": [{"architecture": qarch, "machines": machines}],
            "features": features,
            "tags": [tag, commit],
        }
        (root / "descriptors" / base).write_text(json.dumps(descriptor, indent=4) + "\n")
        written.append(f"{descriptors}/{base}")

manifest = {
    "format": 1,
    "edk2": {"tag": tag, "commit": commit, "url": url, "fixes": [fix]},
    "flags_from": "QEMU v10.0.0 roms/edk2-build.config, plus the front page of edk2-stable202408 (PcdBootManagerMenuFile, PcdSetupConOut*, PcdSetupVideo*)",
    "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "toolchain": toolchain,
    "target": target,
    "compilers": {"x64": gcc, "aarch64": aa64_gcc},
    "firmware": {
        "x64": arch("x64", "q35", "OvmfPkg/OvmfPkgX64.dsc", common.split(),
                    pcds + x64_pcds.split(),
                    "Shell_X64.efi"),
        "aarch64": arch("aarch64", "virt", "ArmVirtPkg/ArmVirtQemu.dsc",
                        common.split() + aa64_opts.split(), pcds + aa64_pcds.split(),
                        "Shell_AARCH64.efi"),
    },
    "descriptors": written,
}
(root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
EOF
sha256sum "$out"/x64/* "$out"/aarch64/*

# install_file <from> <to>: <from> written beside <to> and renamed into it.
install_file() {
    tmp=$(mktemp "$(dirname "$2")/.$(basename "$2").XXXXXX")
    if ! { cp "$1" "$tmp" && chmod 0644 "$tmp" && mv -f "$tmp" "$2"; }; then
        die "cannot install $2"
    fi
    tmp=""
}

if [[ $install == 1 ]]; then
    mkdir -p "$dest/x64" "$dest/aarch64" "$descriptors"
    tmp=""
    trap '[[ -z "$tmp" ]] || rm -f "$tmp"' EXIT
    # The manifest after the images: until it is renamed in, the old one's
    # digests no longer match the new files, so a reader passes the set
    # over. The descriptors last, once the set they name is whole.
    for rel in x64/code.fd x64/vars.fd x64/Shell_X64.efi \
        aarch64/code.fd aarch64/vars.fd aarch64/Shell_AARCH64.efi manifest.json; do
        install_file "$out/$rel" "$dest/$rel"
    done
    for f in "$out"/descriptors/*.json; do
        install_file "$f" "$descriptors/$(basename "$f")"
    done
    # An earlier pin's descriptors would sort first (QEMU's descriptors are
    # read in name order) and keep its set chosen: only this pin's stay.
    # The directory is the pool's own; the earlier sets' files are kept.
    for f in "$descriptors"/10-axlvmpool-*.json; do
        [[ -e "$f" && "$(basename "$f")" != "10-axlvmpool-$tag-"* ]] || continue
        echo "removing an earlier pin's descriptor $f"
        rm -f "$f"
    done
    echo "installed in $dest, descriptors in $descriptors"
fi
