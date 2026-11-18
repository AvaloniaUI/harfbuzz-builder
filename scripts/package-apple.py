#!/usr/bin/env python3
"""Combines the staged slices of an Apple mobile platform into XCFrameworks and packages them (macOS only).

  package-apple.py <ios|tvos|maccatalyst> [--tag TAG]
  package-apple.py <ios|tvos|maccatalyst> --list-slices

Build the slices first with build.py --no-archive:
  ios          ios arm64, ios-simulator arm64, ios-simulator x64
  tvos         tvos arm64, tvos-simulator arm64, tvos-simulator x64
  maccatalyst  maccatalyst arm64, maccatalyst x64

The arm64 and x64 builds of a slice are combined with lipo. The archive is release/harfbuzz-<TAG>-<platform>.tar.gz:
  shared/libnharfbuzz.xcframework   libnharfbuzz.framework per slice (dynamic), with the dSYMs as debug symbols
  static/libnharfbuzz.xcframework   libnharfbuzz.a per slice
and the headers, exports.txt, licenses and build-info.json. Mac Catalyst frameworks use the macOS (versioned) layout.
"""

import argparse
import json
import pathlib
import plistlib
import shutil
import subprocess
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
HARFBUZZ = ROOT / "harfbuzz"
ARTIFACTS = ROOT / "artifacts"
RELEASE = ROOT / "release"

LIBRARY = "libnharfbuzz"

SLICES = {
    "ios": [("ios", ["arm64"]), ("ios-simulator", ["arm64", "x64"])],
    "tvos": [("tvos", ["arm64"]), ("tvos-simulator", ["arm64", "x64"])],
    "maccatalyst": [("maccatalyst", ["arm64", "x64"])],
}

# CFBundleSupportedPlatforms of each slice.
BUNDLE_PLATFORMS = {"ios": "iPhoneOS", "ios-simulator": "iPhoneSimulator", "tvos": "AppleTVOS",
                    "tvos-simulator": "AppleTVSimulator", "maccatalyst": "MacOSX"}

# Mac Catalyst with iOS 15.0 runs on macOS 12.0.
CATALYST_MACOS_VERSION = "12.0"


def run(args, capture=False):
    print("+ " + " ".join(str(a) for a in args), flush=True)
    result = subprocess.run([str(a) for a in args], check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def lipo(inputs, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    if len(inputs) == 1:
        shutil.copy2(inputs[0], output)
    else:
        run(["lipo", "-create", *inputs, "-output", output])


def framework(slice_os, binaries, version, minimum_os, dest):
    """Writes libnharfbuzz.framework for one slice from the lipo of its binaries."""
    root = dest / f"{LIBRARY}.framework"
    catalyst = slice_os == "maccatalyst"
    contents = root / "Versions" / "A" if catalyst else root
    resources = contents / "Resources" if catalyst else root
    resources.mkdir(parents=True)
    lipo(binaries, contents / LIBRARY)
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": LIBRARY,
        "CFBundleIdentifier": f"NHarfBuzz.{LIBRARY}",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": LIBRARY,
        "CFBundlePackageType": "FMWK",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleSupportedPlatforms": [BUNDLE_PLATFORMS[slice_os]],
    }
    if catalyst:
        plist["LSMinimumSystemVersion"] = CATALYST_MACOS_VERSION
    else:
        plist["MinimumOSVersion"] = minimum_os
    with open(resources / "Info.plist", "wb") as f:
        plistlib.dump(plist, f)
    if catalyst:
        (root / "Versions" / "Current").symlink_to("A")
        (root / LIBRARY).symlink_to(f"Versions/Current/{LIBRARY}")
        (root / "Resources").symlink_to("Versions/Current/Resources")
    return root


def dsym(dsyms, dest):
    """Combines the dSYMs of a slice's architectures into one."""
    root = dest / f"{LIBRARY}.framework.dSYM"
    shutil.copytree(dsyms[0], root, symlinks=True)
    dwarf = root / "Contents" / "Resources" / "DWARF"
    for f in dwarf.iterdir():
        f.unlink()
    lipo([d / "Contents" / "Resources" / "DWARF" / LIBRARY for d in dsyms], dwarf / LIBRARY)
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("platform", choices=sorted(SLICES))
    parser.add_argument("--tag", help="release tag (<version>-<sha10>); read from the checkout when omitted")
    parser.add_argument("--list-slices", action="store_true", help="print the slices to build (<os> <arch> per line)")
    args = parser.parse_args()
    if args.list_slices:
        for slice_os, archs in SLICES[args.platform]:
            for arch in archs:
                print(slice_os, arch)
        return

    slices = {}
    infos = {}
    for slice_os, archs in SLICES[args.platform]:
        for arch in archs:
            matches = sorted(ARTIFACTS.glob(f"harfbuzz-{args.tag or '*'}-{slice_os}-{arch}"))
            if len(matches) != 1 or not (matches[0] / "build-info.json").exists():
                sys.exit(f"Missing slice {slice_os}-{arch}: run build.py {slice_os} {arch} --no-archive")
            slices[(slice_os, arch)] = matches[0]
            infos[(slice_os, arch)] = json.loads((matches[0] / "build-info.json").read_text())
    tags = {i["tag"] for i in infos.values()}
    if len(tags) != 1:
        sys.exit(f"The slices have different tags: {sorted(tags)}")
    first = next(iter(infos.values()))
    tag, version = first["tag"], first["version"]

    name = f"harfbuzz-{tag}-{args.platform}"
    stage = ARTIFACTS / name
    work = ARTIFACTS / f"{name}.slices"
    for d in (stage, work):
        if d.exists():
            shutil.rmtree(d)
    first_stage = next(iter(slices.values()))
    for part in ("include", "licenses"):
        shutil.copytree(first_stage / part, stage / part)
    shutil.copy2(first_stage / "exports.txt", stage / "exports.txt")
    for path in slices.values():
        if (path / "exports.txt").read_text() != (stage / "exports.txt").read_text():
            sys.exit(f"{path.name} exports a different API")

    static_inputs, shared_inputs = [], []
    for slice_os, archs in SLICES[args.platform]:
        stages = [slices[(slice_os, arch)] for arch in archs]
        slice_dir = work / slice_os
        static = slice_dir / "static" / f"{LIBRARY}.a"
        lipo([s / "static" / f"{LIBRARY}.a" for s in stages], static)
        static_inputs += ["-library", static]
        minimum_os = infos[(slice_os, archs[0])]["toolchain"]["minimumOS"]
        fw = framework(slice_os, [s / "shared" / LIBRARY for s in stages], version, minimum_os, slice_dir / "shared")
        symbols = dsym([s / "shared" / f"{LIBRARY}.dSYM" for s in stages], slice_dir / "shared")
        shared_inputs += ["-framework", fw, "-debug-symbols", symbols.resolve()]

    (stage / "static").mkdir()
    (stage / "shared").mkdir()
    run(["xcodebuild", "-create-xcframework", *static_inputs, "-output", stage / "static" / f"{LIBRARY}.xcframework"])
    run(["xcodebuild", "-create-xcframework", *shared_inputs, "-output", stage / "shared" / f"{LIBRARY}.xcframework"])
    shutil.rmtree(work)

    info = {key: first[key] for key in ("tag", "repository", "sha", "version", "mesonOptions", "features")}
    info.update({
        "platform": args.platform,
        "rid": args.platform,
        "libraries": {"static": f"static/{LIBRARY}.xcframework", "shared": f"shared/{LIBRARY}.xcframework"},
        "slices": {f"{s}-{a}": {k: i[k] for k in ("rid", "toolchain", "compileFlags", "linkFlags", "libraries")}
                   for (s, a), i in infos.items()},
    })
    (stage / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")

    RELEASE.mkdir(exist_ok=True)
    archive = RELEASE / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=name)
    print(f"Wrote {archive} ({archive.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
