#!/usr/bin/env python3
"""Builds the HarfBuzz checkout in ./harfbuzz (see fetch-harfbuzz.py) with meson, checks it and packages it.

  build.py <linux|macos|windows> <x64|arm64> [--tag TAG]
  build.py android <arm64|x64> --ndk <path>                         (or ANDROID_NDK_HOME)
  build.py wasm wasm32 --emscripten toolchains/emscripten-<band>     (see fetch-emscripten.py)
  build.py <ios|ios-simulator|tvos|tvos-simulator|maccatalyst> <arch> --no-archive   (see package-apple.py)

Meson builds HarfBuzz, harfbuzz-subset, -raster, -vector and -gpu as static libraries. This script merges them into
one static library (libnharfbuzz.a, libnharfbuzz.lib on Windows) and links that into one shared library
(libnharfbuzz.so, libnharfbuzz.dll, libnharfbuzz.dylib) that exports the hb_* functions of the public headers and
nothing else. WebAssembly has the static library only. Apple mobile slices link the shared library as the binary of
libnharfbuzz.framework, which package-apple.py assembles.

The checks compare the exported symbols with the public API, compare the dynamic dependencies with an allowed list,
and compile scripts/smoke/smoke.c against the staged headers and both libraries. The smoke test runs when the build
machine can run the target (WebAssembly runs under Node).

The archive is release/harfbuzz-<TAG>-<platform>.tar.gz, where the platform is <os>-<arch>, or
wasm-emscripten-<version> for WebAssembly. It holds include/harfbuzz, static/, shared/ (with separate debug symbols),
exports.txt, licenses/ and build-info.json.
"""

import argparse
import contextlib
import json
import os
import pathlib
import platform as host
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
HARFBUZZ = ROOT / "harfbuzz"
BUILD = ROOT / "build"
ARTIFACTS = ROOT / "artifacts"
RELEASE = ROOT / "release"
SMOKE = ROOT / "scripts" / "smoke" / "smoke.c"
SMOKE_FONT = "test/subset/data/fonts/Roboto-Regular.ttf"

LIBRARY = "libnharfbuzz"

ARCHES = {
    "linux": {"x64", "arm64"},
    "macos": {"x64", "arm64"},
    "windows": {"x64", "arm64"},
    "android": {"x64", "arm64"},
    "wasm": {"wasm32"},
    "ios": {"arm64"},
    "ios-simulator": {"arm64", "x64"},
    "tvos": {"arm64"},
    "tvos-simulator": {"arm64", "x64"},
    "maccatalyst": {"arm64", "x64"},
}
APPLE_SLICES = {"ios", "ios-simulator", "tvos", "tvos-simulator", "maccatalyst"}

RIDS = {"linux": "linux", "macos": "osx", "windows": "win", "android": "android", "ios": "ios",
        "ios-simulator": "iossimulator", "tvos": "tvos", "tvos-simulator": "tvossimulator",
        "maccatalyst": "maccatalyst"}

MACOS_MIN_VERSION = "11.0"
APPLE_MIN_VERSION = "15.0"
ANDROID_API = 24

# Core shaping, subset, raster, vector and GPU, without experimental API. auto_features=disabled turns off every
# optional dependency (GLib, ICU, FreeType, Cairo, PNG, zlib, ...) that is not enabled here.
MESON_OPTIONS = {
    "optimization": "2",
    "debug": "false",
    "b_ndebug": "true",
    "default_library": "static",
    "auto_features": "disabled",
    "subset": "enabled",
    "raster": "enabled",
    "vector": "enabled",
    "gpu": "enabled",
    "tests": "disabled",
    "utilities": "disabled",
    "docs": "disabled",
    "introspection": "disabled",
    "benchmark": "disabled",
    "experimental_api": "false",
    "with_libstdcxx": "false",
}

# The meson libraries merged into libnharfbuzz, core first. Each one has a <name>.def target listing its public API.
COMPONENTS = ["harfbuzz", "harfbuzz-subset", "harfbuzz-raster", "harfbuzz-vector", "harfbuzz-gpu"]

APPLE_FRAMEWORKS = ["CoreText", "CoreGraphics", "CoreFoundation"]

# Dynamic dependencies the shared library may have.
ALLOWED_DEPENDENCIES = {
    "linux": {"libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2"},
    "android": {"libc.so", "libm.so", "libdl.so"},
    "apple": {"/usr/lib/libSystem.B.dylib"} | {f"/System/Library/Frameworks/{f}.framework/{v}{f}" for f in
                                                APPLE_FRAMEWORKS for v in ("", "Versions/A/")},
    "windows": {"kernel32.dll", "dwrite.dll"},
}


def run(args, cwd=None, env=None, capture=False, quiet=False):
    args = [str(a) for a in args]
    if not quiet:
        print("+ " + " ".join(args), flush=True)
    result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def which(*names):
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    sys.exit(f"None of {', '.join(names)} is on PATH")


def host_arch():
    machine = host.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def first_line(args):
    return subprocess.run([str(a) for a in args], check=True, text=True, capture_output=True).stdout.splitlines()[0]


class Target:
    """The compilers, tools and flags of one build."""

    def __init__(self, os_name, arch, args):
        self.os = os_name
        self.arch = arch
        self.cpu = {"x64": "x86_64", "arm64": "aarch64", "wasm32": "wasm32"}[arch]
        self.platform = f"{os_name}-{arch}"
        self.rid = f"{RIDS.get(os_name, os_name)}-{arch}"
        self.toolchain = {}
        self.env = dict(os.environ)
        self.flags = []          # C and C++ compile flags
        self.link_flags = []
        self.options = dict(MESON_OPTIONS)
        self.apple = os_name == "macos" or os_name in APPLE_SLICES
        self.kind = "apple" if self.apple else {"windows": "windows", "wasm": "wasm", "android": "android"}.get(os_name, "linux")
        getattr(self, f"setup_{self.kind}")(args)

    # Linux: Clang (in docker/linux/Dockerfile: Clang 20, Ubuntu 20.04).
    def setup_linux(self, args):
        cc = os.environ.get("HARFBUZZ_BUILDER_CC", "clang-20")
        cxx = os.environ.get("HARFBUZZ_BUILDER_CXX", "clang++-20")
        suffix = cc[len("clang"):] if cc.startswith("clang") else ""
        self.cc, self.cxx = which(cc), which(cxx)
        self.ar, self.nm, self.objcopy, self.readelf = (which(f"llvm-{t}{suffix}", f"llvm-{t}") for t in
                                                         ("ar", "nm", "objcopy", "readelf"))
        self.system = "linux"
        self.flags = ["-ffunction-sections", "-fdata-sections", "-g"]
        if self.arch == "arm64":
            self.flags.append("-mno-outline-atomics")
        self.link_flags = ["-Wl,-z,relro", "-Wl,-z,now", "-Wl,--build-id=sha1"]
        self.toolchain = {"compiler": first_line([self.cc, "--version"])}

    def setup_android(self, args):
        if not args.ndk or not pathlib.Path(args.ndk).is_dir():
            sys.exit("The Android build needs an NDK: pass --ndk or set ANDROID_NDK_HOME")
        ndk = pathlib.Path(args.ndk).resolve()
        hosts = list((ndk / "toolchains" / "llvm" / "prebuilt").iterdir())
        bin_dir = hosts[0] / "bin"
        triple = {"arm64": "aarch64-linux-android", "x64": "x86_64-linux-android"}[self.arch]
        self.cc, self.cxx = bin_dir / "clang", bin_dir / "clang++"
        self.ar, self.nm, self.objcopy, self.readelf = (bin_dir / f"llvm-{t}" for t in ("ar", "nm", "objcopy", "readelf"))
        self.system = "android"
        self.flags = [f"--target={triple}{ANDROID_API}", "-ffunction-sections", "-fdata-sections", "-g"]
        # 16 KB pages: Google Play requires 16 KB-aligned libraries for apps that target Android 15 and later.
        self.link_flags = [f"--target={triple}{ANDROID_API}", "-Wl,-z,max-page-size=16384", "-Wl,-z,relro",
                           "-Wl,-z,now", "-Wl,--build-id=sha1"]
        props = (ndk / "source.properties").read_text()
        revision = next((l.split("=", 1)[1].strip() for l in props.splitlines() if l.startswith("Pkg.Revision")), "")
        self.toolchain = {"ndk": revision, "androidApi": ANDROID_API, "compiler": first_line([self.cc, "--version"])}

    def setup_wasm(self, args):
        if args.emscripten is None:
            sys.exit("The WebAssembly build needs --emscripten (a directory written by fetch-emscripten.py)")
        toolchain = json.loads((args.emscripten / "toolchain.json").read_text())
        self.env.update(toolchain.pop("environment"))
        emscripten = pathlib.Path(toolchain.pop("emscriptenDir"))
        llvm = pathlib.Path(self.env["DOTNET_EMSCRIPTEN_LLVM_ROOT"])
        self.cc, self.cxx = emscripten / "emcc", emscripten / "em++"
        self.ar, self.nm = llvm / "llvm-ar", llvm / "llvm-nm"
        self.node = self.env["DOTNET_EMSCRIPTEN_NODE_JS"]
        self.system = "emscripten"
        self.flags = ["-msimd128", "-ffunction-sections", "-fdata-sections", "-DHB_NO_MT"]
        self.platform = f"wasm-emscripten-{toolchain['emscripten']}"
        self.rid = f"browser-wasm-emscripten-{toolchain['emscripten']}"
        self.toolchain = toolchain

    def setup_apple(self, args):
        simulator = self.os.endswith("-simulator")
        family = self.os.removesuffix("-simulator")
        cpu = {"x64": "x86_64", "arm64": "arm64"}[self.arch]
        sdk_name = {"macos": "macosx", "ios": "iphonesimulator" if simulator else "iphoneos",
                    "tvos": "appletvsimulator" if simulator else "appletvos", "maccatalyst": "macosx"}[family]
        sdk = first_line(["xcrun", "--sdk", sdk_name, "--show-sdk-path"])
        if family == "macos":
            triple = f"{cpu}-apple-macos{MACOS_MIN_VERSION}"
        elif family == "maccatalyst":
            triple = f"{cpu}-apple-ios{APPLE_MIN_VERSION}-macabi"
        else:
            triple = f"{cpu}-apple-{family}{APPLE_MIN_VERSION}{'-simulator' if simulator else ''}"
        tool = lambda name: first_line(["xcrun", "--sdk", sdk_name, "--find", name])
        self.cc, self.cxx = tool("clang"), tool("clang++")
        self.ar, self.nm, self.strip, self.libtool = tool("ar"), tool("nm"), tool("strip"), tool("libtool")
        self.dsymutil, self.otool = tool("dsymutil"), tool("otool")
        self.system = "darwin"
        self.flags = [f"--target={triple}", "-isysroot", sdk, "-ffunction-sections", "-fdata-sections", "-g"]
        if family == "maccatalyst":
            self.flags += ["-isystem", f"{sdk}/System/iOSSupport/usr/include",
                           "-iframework", f"{sdk}/System/iOSSupport/System/Library/Frameworks"]
        self.link_flags = [f"--target={triple}", "-isysroot", sdk]
        if family == "maccatalyst":
            self.link_flags += ["-iframework", f"{sdk}/System/iOSSupport/System/Library/Frameworks"]
        self.options["coretext"] = "enabled"
        self.toolchain = {"compiler": first_line([self.cc, "--version"]), "sdk": pathlib.Path(sdk).name,
                          "target": triple}
        self.toolchain["minimumOS"] = MACOS_MIN_VERSION if family == "macos" else APPLE_MIN_VERSION

    # Windows: clang-cl and lld-link with the static CRT, in the environment of vcvarsall for the target.
    def setup_windows(self, args):
        llvm = pathlib.Path(os.environ.get("HARFBUZZ_BUILDER_LLVM", "C:/Program Files/LLVM")) / "bin"
        self.env = msvc_environment(self.arch)
        self.env["PATH"] = str(llvm) + os.pathsep + self.env["PATH"]
        self.cc = self.cxx = llvm / "clang-cl.exe"
        self.ld, self.lib, self.ar = llvm / "lld-link.exe", llvm / "llvm-lib.exe", llvm / "llvm-ar.exe"
        self.objcopy = llvm / "llvm-objcopy.exe"
        self.readobj, self.nm = llvm / "llvm-readobj.exe", llvm / "llvm-nm.exe"
        self.system = "windows"
        # --target is part of the compiler command: meson reads the target of clang-cl (and so the /MACHINE of
        # llvm-lib and lld-link) from its --version output.
        self.target_flag = "--target=" + {"x64": "x86_64-pc-windows-msvc", "arm64": "aarch64-pc-windows-msvc"}[self.arch]
        self.flags = ["/Z7", "/Gy", "/Gw", "/guard:cf"]
        self.machine = {"x64": "X64", "arm64": "ARM64"}[self.arch]
        self.options["b_vscrt"] = "mt"
        self.options["directwrite"] = "enabled"
        self.toolchain = {"compiler": first_line([self.cc, "--version"]), "crt": "static (/MT)"}
        if not self.constexpr_nan():
            self.flags.append(f"/FI{(ROOT / 'scripts' / 'windows' / 'constexpr-nan.h').as_posix()}")
            self.toolchain["constexprNanShim"] = True

    def constexpr_nan(self):
        """Whether NAN of this Windows SDK is a constant expression for clang-cl (see windows/constexpr-nan.h)."""
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "nan.cpp"
            source.write_text("#include <math.h>\nconstexpr float value = NAN;\n")
            result = subprocess.run([str(self.cc), "/nologo", "/Zs", self.target_flag, str(source)], env=self.env,
                                    capture_output=True)
            return result.returncode == 0

    def cross_file(self, path):
        def value(v):
            if isinstance(v, list):
                return "[" + ", ".join(value(x) for x in v) + "]"
            return "'" + str(v).replace("\\", "/").replace("'", "\\'") + "'"

        binaries = {"c": self.cc, "cpp": self.cxx, "ar": self.lib if self.kind == "windows" else self.ar}
        if self.kind == "windows":
            binaries.update({"c": [self.cc, self.target_flag], "cpp": [self.cxx, self.target_flag],
                             "c_ld": self.ld, "cpp_ld": self.ld})
        lines = ["[binaries]"] + [f"{k} = {value(v)}" for k, v in binaries.items()]
        lines += ["", "[built-in options]",
                  f"c_args = {value(self.flags)}",
                  f"cpp_args = {value(self.flags)}",
                  f"c_link_args = {value(self.link_flags)}",
                  f"cpp_link_args = {value(self.link_flags)}"]
        lines += ["", "[host_machine]", f"system = '{self.system}'", f"cpu_family = '{self.cpu}'",
                  f"cpu = '{self.cpu}'", "endian = 'little'"]
        path.write_text("\n".join(lines) + "\n")

    def can_run(self):
        """Whether the smoke test can run here (it is only linked otherwise)."""
        if self.kind == "wasm":
            return True
        here = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform)
        if self.os != here:
            return False
        if self.arch == host_arch():
            return True
        if self.os == "macos" and self.arch == "x64":
            return subprocess.run(["arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode == 0
        return self.os == "windows" and self.arch == "x64"


# Under Emscripten, meson's threads dependency is -pthread, which compiles atomics and shared memory that
# single-threaded WebAssembly hosts (.NET) cannot link. WebAssembly builds skip it and define HB_NO_MT.
NO_THREADS = ("if host_machine.system() != 'windows'\n  thread_dep = dependency('threads', required: false)",
              "if host_machine.system() not in ['windows', 'emscripten']\n  thread_dep = dependency('threads', required: false)")


@contextlib.contextmanager
def without_threads():
    """Applies NO_THREADS to the checkout's meson.build for the duration of a build."""
    file = HARFBUZZ / "meson.build"
    original = file.read_text()
    if NO_THREADS[0] not in original:
        sys.exit(f"{file} no longer contains the threads dependency that WebAssembly builds remove")
    file.write_text(original.replace(NO_THREADS[0], NO_THREADS[1]))
    try:
        yield
    finally:
        file.write_text(original)


def check_single_threaded(library):
    """Fails when an object of the WebAssembly library uses atomics (its target_features section lists +atomics)."""
    if b"+\x07atomics" in library.read_bytes():
        sys.exit(f"{library} uses WebAssembly atomics; single-threaded hosts cannot link it")


def msvc_environment(arch):
    """The environment of vcvarsall.bat for a build on this machine that targets arch."""
    vswhere = pathlib.Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"),
                           "Microsoft Visual Studio/Installer/vswhere.exe")
    install = first_line([vswhere, "-latest", "-products", "*", "-requires",
                          "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"])
    vcvars = pathlib.Path(install) / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    host_name = {"x64": "x64", "arm64": "arm64"}[host_arch()]
    target_name = {"x64": "amd64", "arm64": "arm64"}[arch]
    spec = host_name if (host_name, target_name) in (("x64", "amd64"), ("arm64", "arm64")) else f"{host_name}_{target_name}"
    out = subprocess.run(f'cmd /s /c ""{vcvars}" {spec} >nul && set"', shell=True, check=True, text=True,
                         capture_output=True).stdout
    # Variable names are case-insensitive on Windows; os.environ uses upper case too.
    return {k.upper(): v for k, v in (line.split("=", 1) for line in out.splitlines() if "=" in line)}


def global_symbols(target, obj):
    out = run([target.nm, "-g", "--defined-only", "-j", obj], capture=True, quiet=True)
    return set(out.split())


def merge_static(target, libraries, output, work):
    """Merges the static libraries into one.

    Meson compiles some sources (hb-static.cc, and core sources that harfbuzz-subset uses) into more than one library,
    with the same arguments. The merged library keeps the first copy of each object, after checking that the other
    copies define the same symbols.
    """
    members = []
    seen = {}
    for index, library in enumerate(libraries):
        folder = work / f"{index}-{library.stem}"
        folder.mkdir(parents=True)
        # Members are extracted by base name; lib.exe archives store them with a relative path.
        names = [n.replace("\\", "/").rsplit("/", 1)[-1] for n in
                 run([target.ar, "t", library], capture=True, quiet=True).splitlines()
                 if n and not n.startswith("__.SYMDEF")]
        if len(set(names)) != len(names):
            sys.exit(f"{library} has two members with the same name")
        if target.apple:
            run([target.ar, "x", library], cwd=folder, quiet=True)
        else:
            run([target.ar, "x", f"--output={folder}", library], quiet=True)
        for name in names:
            if name in seen:
                if global_symbols(target, seen[name]) != global_symbols(target, folder / name):
                    sys.exit(f"{name} of {library.name} differs from the copy in another library")
                continue
            seen[name] = folder / name
            members.append(folder / name)
    output.unlink(missing_ok=True)
    rsp = work / "members.rsp"
    rsp.write_text("\n".join(str(m).replace("\\", "/") for m in members) + "\n")
    if target.kind == "windows":
        run([target.lib, "/nologo", f"/out:{output}", f"@{rsp}"])
    elif target.apple:
        run([target.libtool, "-static", "-no_warning_for_no_symbols", "-o", output, "-filelist", rsp])
    else:
        run([target.ar, "rcs", output, f"@{rsp}"])


def strip_static(target, library, output):
    """Copies the static library without debug information (the shared library has separate debug symbols)."""
    if target.kind == "wasm":
        shutil.copy2(library, output)
    elif target.apple:
        shutil.copy2(library, output)
        run([target.strip, "-S", output])
    else:
        run([target.objcopy, "--strip-debug", library, output])


def link_shared(target, static_lib, exports, shared_dir, work, version, install_name=None):
    """Links the shared library and moves its debug information to a separate file. Returns the library."""
    shared_dir.mkdir(parents=True, exist_ok=True)
    if target.kind == "windows":
        dll = shared_dir / f"{LIBRARY}.dll"
        definition = work / f"{LIBRARY}.def"
        definition.write_text("LIBRARY " + dll.name + "\nEXPORTS\n" + "".join(f"  {e}\n" for e in exports))
        run([target.ld, "/nologo", "/DLL", f"/MACHINE:{target.machine}", f"/OUT:{dll}",
             f"/IMPLIB:{shared_dir / (LIBRARY + '.lib')}", f"/PDB:{shared_dir / (LIBRARY + '.pdb')}",
             "/DEBUG:FULL", f"/DEF:{definition}", "/OPT:REF", "/OPT:ICF", "/GUARD:CF", "/DYNAMICBASE", "/NXCOMPAT",
             "/Brepro", static_lib, "dwrite.lib"], env=target.env)
        exp = shared_dir / f"{LIBRARY}.exp"
        exp.unlink(missing_ok=True)
        return dll

    if target.apple:
        name = install_name.rsplit("/", 1)[1] if install_name else f"{LIBRARY}.dylib"
        library = shared_dir / name
        exported = work / "exported_symbols.txt"
        exported.write_text("".join(f"_{e}\n" for e in exports))
        undefined = work / "undefined.rsp"
        undefined.write_text("".join(f"-Wl,-u,_{e}\n" for e in exports))
        major = version.split(".")[0]
        run([target.cc, "-dynamiclib", *target.link_flags, "-o", library,
             f"-Wl,-install_name,{install_name or '@rpath/' + name}", f"-Wl,-exported_symbols_list,{exported}",
             f"@{undefined}", "-Wl,-dead_strip", "-Wl,-headerpad_max_install_names",
             f"-Wl,-current_version,{version}", f"-Wl,-compatibility_version,{major}.0.0", static_lib,
             *[a for f in APPLE_FRAMEWORKS for a in ("-framework", f)]])
        dsym = shared_dir / f"{name}.dSYM"
        if dsym.exists():
            shutil.rmtree(dsym)
        run([target.dsymutil, library, "-o", dsym])
        run([target.strip, "-x", library])
        return library

    library = shared_dir / f"{LIBRARY}.so"
    script = work / f"{LIBRARY}.map"
    script.write_text("{\n  global:\n" + "".join(f"    {e};\n" for e in exports) + "  local:\n    *;\n};\n")
    undefined = work / "undefined.rsp"
    undefined.write_text("".join(f"-Wl,--undefined={e}\n" for e in exports))
    libs = ["-lm"] + (["-pthread"] if target.kind == "linux" else [])
    run([target.cc, "-shared", *target.link_flags, "-o", library, f"-Wl,-soname,{library.name}",
         f"-Wl,--version-script={script}", f"@{undefined}", "-Wl,-Bsymbolic", "-Wl,--no-undefined",
         "-Wl,--gc-sections", static_lib, *libs])
    debug = shared_dir / f"{library.name}.debug"
    run([target.objcopy, "--only-keep-debug", "--compress-debug-sections=zlib", library, debug])
    run([target.objcopy, "--strip-all", f"--add-gnu-debuglink={debug}", library])
    return library


def exported_symbols(target, library):
    if target.kind == "windows":
        out = run([target.readobj, "--coff-exports", library], capture=True, quiet=True)
        return {m for m in re.findall(r"^\s*Name: (\S+)$", out, re.M)}
    if target.apple:
        out = run([target.nm, "-gU", "-j", library], capture=True, quiet=True)
        return {s[1:] for s in out.split()}
    out = run([target.nm, "-D", "--defined-only", "-j", library], capture=True, quiet=True)
    return set(out.split())


def dependencies(target, library):
    if target.kind == "windows":
        out = run([target.readobj, "--coff-imports", library], capture=True, quiet=True)
        return sorted({m.lower() for m in re.findall(r"^\s*Name: (\S+)$", out, re.M)})
    if target.apple:
        out = run([target.otool, "-L", library], capture=True, quiet=True).splitlines()[1:]
        names = [line.strip().split(" (")[0] for line in out]
        return sorted(n for n in names if not n.startswith("@rpath/"))
    out = run([target.readelf, "-d", library], capture=True, quiet=True)
    return sorted(re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", out))


def check_shared(target, library, exports):
    """Fails unless the library exports exactly the public API and depends only on allowed libraries."""
    found = exported_symbols(target, library)
    missing, extra = sorted(set(exports) - found), sorted(found - set(exports))
    if missing or extra:
        sys.exit(f"{library.name} exports do not match the public API: missing {missing[:20]} "
                 f"({len(missing)}), unexpected {extra[:20]} ({len(extra)})")
    deps = dependencies(target, library)
    allowed = ALLOWED_DEPENDENCIES[target.kind]
    if target.kind == "windows":
        allowed = {d.lower() for d in allowed}
    unexpected = [d for d in deps if d not in allowed]
    if unexpected:
        sys.exit(f"{library.name} has unexpected dependencies: {unexpected}")
    info = {"exports": len(found), "dependencies": deps}
    if target.kind in ("linux", "android"):
        headers = run([target.readelf, "-lW", library], capture=True, quiet=True)
        aligns = [int(a, 16) for a in re.findall(r"^\s*LOAD\s.*\s(0x[0-9a-f]+)$", headers, re.M)]
        info["loadAlignment"] = min(aligns)
        if target.kind == "android" and min(aligns) < 16384:
            sys.exit(f"{library.name} LOAD segments are not 16 KB aligned: {aligns}")
        if target.kind == "linux":
            symbols = run([target.readelf, "--dyn-syms", "-W", library], capture=True, quiet=True)
            versions = {tuple(int(x) for x in v.split(".")) for v in re.findall(r"@GLIBC_([0-9.]+)", symbols)}
            info["glibc"] = ".".join(map(str, max(versions))) if versions else None
    print(f"{library.name}: {len(found)} exports, dependencies {deps}")
    return info


def smoke(target, stage, version, work):
    """Compiles smoke.c against the staged headers with each library and runs it when this machine can."""
    font = HARFBUZZ / SMOKE_FONT
    include = stage / "include" / "harfbuzz"
    runnable = target.can_run()
    results = {}
    variants = ["static"] if target.kind == "wasm" else ["shared", "static"]
    for variant in variants:
        out = work / f"smoke-{variant}"
        out.mkdir(parents=True, exist_ok=True)
        if target.kind == "windows":
            exe = out / "smoke.exe"
            lib = stage / variant / f"{LIBRARY}.lib"
            # As C++ (/TP): hb-directwrite.h includes dwrite.h, which is C++ only.
            run([target.cc, "/nologo", "/MT", "/O1", "/TP", target.target_flag, f"/I{include}", SMOKE, f"/Fo{out}\\",
                 f"/Fe{exe}", "/link", lib, "dwrite.lib"], env=target.env)
            if variant == "shared":
                shutil.copy2(stage / "shared" / f"{LIBRARY}.dll", out)
            command = [exe]
        elif target.kind == "wasm":
            exe = out / "smoke.js"
            run([target.cc, *target.flags, "-O1", f"-I{include}", SMOKE, stage / "static" / f"{LIBRARY}.a",
                 "-sENVIRONMENT=node", "-sNODERAWFS=1", "-sALLOW_MEMORY_GROWTH=1", "-sEXIT_RUNTIME=1",
                 "-o", exe], env=target.env)
            command = [target.node, exe]
        else:
            exe = out / "smoke"
            flags = [f for f in target.flags if f not in ("-ffunction-sections", "-fdata-sections", "-g")]
            if variant == "shared":
                if target.os in APPLE_SLICES:
                    libs = [stage / "shared" / LIBRARY]
                else:
                    libs = [f"-L{stage / 'shared'}", "-lnharfbuzz", f"-Wl,-rpath,{stage / 'shared'}"]
            else:
                libs = [stage / "static" / f"{LIBRARY}.a"]
                libs += [a for f in APPLE_FRAMEWORKS for a in ("-framework", f)] if target.apple else ["-lm"]
                libs += ["-pthread"] if target.kind == "linux" else []
            run([target.cc, *flags, *target.link_flags, "-O1", f"-I{include}", SMOKE, "-o", exe, *libs], env=target.env)
            command = [exe]
            if target.os == "macos" and target.arch != host_arch():
                command = ["arch", "-x86_64", exe]
        if runnable:
            run([*command, font, version], cwd=out, env=target.env)
            results[variant] = "ran"
        else:
            results[variant] = "linked"
    return results


def features(header):
    return sorted(re.findall(r"^#define (HB_HAS_\w+) 1", header.read_text(), re.M))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("os", choices=sorted(ARCHES))
    parser.add_argument("arch", choices=["x64", "arm64", "wasm32"])
    parser.add_argument("--tag", help="release tag (<version>-<sha10>); read from the checkout when omitted")
    parser.add_argument("--skip-build", action="store_true", help="package an existing meson build")
    parser.add_argument("--ndk", default=os.environ.get("ANDROID_NDK_HOME") or os.environ.get("ANDROID_NDK_ROOT"),
                        help="Android NDK (default: ANDROID_NDK_HOME or ANDROID_NDK_ROOT)")
    parser.add_argument("--emscripten", type=pathlib.Path, help="toolchain directory written by fetch-emscripten.py")
    parser.add_argument("--no-archive", action="store_true", help="stage the build in artifacts/ without a tar.gz")
    args = parser.parse_args()
    if args.arch not in ARCHES[args.os]:
        sys.exit(f"{args.os} builds {', '.join(sorted(ARCHES[args.os]))}")
    if not (HARFBUZZ / "meson.build").exists():
        sys.exit("No HarfBuzz checkout in ./harfbuzz: run scripts/fetch-harfbuzz.py first")

    sha = run(["git", "rev-parse", "HEAD"], cwd=HARFBUZZ, capture=True, quiet=True).strip()
    version = re.search(r"version\s*:\s*'([0-9.]+)'", (HARFBUZZ / "meson.build").read_text()).group(1)
    tag = args.tag or f"{version}-{sha[:10]}"
    target = Target(args.os, args.arch, args)

    build_dir = BUILD / target.platform
    meson_dir = build_dir / "meson"
    install_dir = build_dir / "install"
    work = build_dir / "work"
    meson = which("meson")
    ninja = which("ninja")
    if not args.skip_build:
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir(parents=True)
        cross = build_dir / "cross.ini"
        target.cross_file(cross)
        options = [f"-D{k}={v}" for k, v in target.options.items()]
        with without_threads() if target.kind == "wasm" else contextlib.nullcontext():
            run([meson, "setup", meson_dir, HARFBUZZ, "--cross-file", cross, f"--prefix={install_dir}",
                 "--libdir=lib", "--includedir=include", *options], env=target.env)
            run([ninja, "-C", meson_dir], env=target.env)
            run([ninja, "-C", meson_dir, *(f"src/{c}.def" for c in COMPONENTS)], env=target.env)
            run([meson, "install", "-C", meson_dir, "--no-rebuild", "--quiet"], env=target.env)

    exports = sorted({line for c in COMPONENTS for line in (meson_dir / "src" / f"{c}.def").read_text().split()
                      if re.fullmatch(r"hb_\w+", line)})

    name = f"harfbuzz-{tag}-{target.platform}"
    stage = ARTIFACTS / name
    for d in (stage, work):
        if d.exists():
            shutil.rmtree(d)
    work.mkdir(parents=True)
    shutil.copytree(install_dir / "include" / "harfbuzz", stage / "include" / "harfbuzz")
    (stage / "licenses" / "harfbuzz").mkdir(parents=True)
    shutil.copy2(HARFBUZZ / "COPYING", stage / "licenses" / "harfbuzz" / "COPYING")
    (stage / "exports.txt").write_text("".join(f"{e}\n" for e in exports))

    libs_dir = install_dir / "lib"
    components = []
    for component in COMPONENTS:
        found = [p for p in (libs_dir / f"lib{component}.a", libs_dir / f"{component}.lib") if p.exists()]
        if not found:
            sys.exit(f"meson did not build {component}")
        components.append(found[0])
    static_name = f"{LIBRARY}.lib" if target.kind == "windows" else f"{LIBRARY}.a"
    static_lib = work / static_name
    merge_static(target, components, static_lib, work / "merge")
    (stage / "static").mkdir()
    strip_static(target, static_lib, stage / "static" / static_name)
    if target.kind == "wasm":
        check_single_threaded(stage / "static" / static_name)

    info = {"static": f"static/{static_name}"}
    if target.kind != "wasm":
        install_name = None
        if target.os in APPLE_SLICES:
            framework = f"@rpath/{LIBRARY}.framework"
            install_name = f"{framework}/Versions/A/{LIBRARY}" if target.os == "maccatalyst" else f"{framework}/{LIBRARY}"
        shared = link_shared(target, static_lib, exports, stage / "shared", work, version, install_name)
        info["shared"] = f"shared/{shared.name}"
        info.update(check_shared(target, shared, exports))
    info["smoke"] = smoke(target, stage, version, work / "smoke")

    build_info = {
        "tag": tag,
        "repository": os.environ.get("HARFBUZZ_BUILDER_REPOSITORY", "harfbuzz/harfbuzz"),
        "sha": sha,
        "version": version,
        "os": args.os,
        "arch": args.arch,
        "platform": target.platform,
        "rid": target.rid,
        "toolchain": dict(target.toolchain, meson=first_line([meson, "--version"])),
        "mesonOptions": target.options,
        "compileFlags": ([target.target_flag] if target.kind == "windows" else []) + target.flags,
        "linkFlags": target.link_flags,
        "features": features(stage / "include" / "harfbuzz" / "hb-features.h"),
        "libraries": info,
    }
    (stage / "build-info.json").write_text(json.dumps(build_info, indent=2) + "\n")
    if args.no_archive:
        print(f"Staged {stage}")
        return

    RELEASE.mkdir(exist_ok=True)
    archive = RELEASE / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=name)
    print(f"Wrote {archive} ({archive.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
