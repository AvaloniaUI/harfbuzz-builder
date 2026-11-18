# HarfBuzz Builder

Builds HarfBuzz as `libnharfbuzz`, a standalone native library for a .NET binding, and publishes it as GitHub
releases. Each platform gets a shared library that exports the HarfBuzz C API and nothing else, and a static library
with the same content.

## Releases

Run the **Build HarfBuzz** workflow (Actions → Build HarfBuzz → Run workflow) with a HarfBuzz version:

- a tag, such as `15.1.0`,
- a branch, such as `main`,
- a full commit SHA.

The workflow resolves the version to a commit and builds Linux, Windows, macOS and Android for x64 and arm64, iOS,
tvOS and Mac Catalyst as XCFrameworks, and WebAssembly once for each Emscripten version listed below. When every
build succeeds, it publishes the release `<version>-<sha10>` (for example `15.1.0-3df40d9b98`). The version is the
project version in HarfBuzz's `meson.build` at that commit. A run fails if that release exists.

Each release has:

| Asset | Content |
|---|---|
| `harfbuzz-<tag>-<os>-<arch>.tar.gz` | See [Archive layout](#archive-layout). |
| `harfbuzz-<tag>-<ios\|tvos\|maccatalyst>.tar.gz` | The headers, `shared/libnharfbuzz.xcframework` (a dynamic `libnharfbuzz.framework` per slice, with its dSYM), `static/libnharfbuzz.xcframework` and `build-info.json` with each slice. |
| `harfbuzz-<tag>-wasm-emscripten-<version>.tar.gz` | The headers and `static/libnharfbuzz.a`, built with that Emscripten version. |
| `SHA256SUMS` | The SHA-256 of each archive. |
| `manifest.json` | The commit, version and each archive by RID (`linux-x64`, `osx-arm64`, `win-x64`, `android-arm64`, `ios`, `browser-wasm-emscripten-3.1.56`, ...) with its size and SHA-256. |

### Archive layout

```
harfbuzz-<tag>-<os>-<arch>/
  include/harfbuzz/       public headers, including hb-features.h of this build
  static/libnharfbuzz.a   all HarfBuzz libraries in one archive, without debug information (libnharfbuzz.lib on Windows)
  shared/                 the shared library and its debug symbols
  exports.txt             the exported functions, one per line
  licenses/harfbuzz/COPYING
  build-info.json         commit, toolchain, meson options, flags, features, exports, dependencies, checks
```

| OS | `shared/` |
|---|---|
| Linux, Android | `libnharfbuzz.so` (soname `libnharfbuzz.so`), `libnharfbuzz.so.debug` (zlib-compressed DWARF, linked with `.gnu_debuglink`) |
| Windows | `libnharfbuzz.dll`, its import library `libnharfbuzz.lib`, `libnharfbuzz.pdb` |
| macOS | `libnharfbuzz.dylib` (install name `@rpath/libnharfbuzz.dylib`), `libnharfbuzz.dylib.dSYM` |

The iOS and tvOS frameworks use the flat bundle layout (install name `@rpath/libnharfbuzz.framework/libnharfbuzz`).
Mac Catalyst frameworks use the versioned macOS layout (`@rpath/libnharfbuzz.framework/Versions/A/libnharfbuzz`).

## Builds

All builds use meson with `optimization=2`, `b_ndebug=true`, no experimental API, and `auto_features=disabled`. Only
these libraries and integrations are enabled:

- HarfBuzz (shaping, with the built-in Unicode and OpenType functions), harfbuzz-subset, harfbuzz-raster,
  harfbuzz-vector and harfbuzz-gpu.
- CoreText on macOS, iOS, tvOS and Mac Catalyst. DirectWrite on Windows.

There is no FreeType, ICU, GLib, Cairo, PNG or zlib. HarfBuzz does not link a C++ standard library
(`with_libstdcxx=false`).

| OS | Compiler | Dependencies of the shared library |
|---|---|---|
| Linux | Clang 20 on Ubuntu 20.04 (glibc 2.31), in `docker/linux/Dockerfile` | `libc.so.6`, `libm.so.6`, `libpthread.so.0` |
| Windows | clang-cl and lld-link, static CRT (`/MT`), `/guard:cf` | `kernel32.dll`, `dwrite.dll` |
| macOS | Xcode Clang, macOS 11.0 | `libSystem`, CoreText, CoreGraphics, CoreFoundation |
| Android | NDK r27d Clang, API level 24, 16 KB page alignment | `libc.so`, `libm.so`, `libdl.so` |
| iOS, tvOS | Xcode Clang, iOS and tvOS 15.0; device arm64, simulator arm64 and x86_64 | as macOS |
| Mac Catalyst | Xcode Clang, iOS 15.0 on Mac (`-macabi`), arm64 and x86_64 | as macOS |
| WebAssembly | Emscripten of a .NET SDK's wasm-tools workload, wasm SIMD (`-msimd128`), no threads | (static only) |

### How libnharfbuzz is made

Meson builds each HarfBuzz library as a static library. `build.py` merges them into `libnharfbuzz.a`. Meson
compiles some sources into more than one library (`hb-static.cc`, and core sources that harfbuzz-subset also
uses), so the merged library keeps the first copy of each object. The script first checks that the other copies
define the same symbols.

The shared library is linked from the merged library. Its export list is the union of the `harfbuzz*.def` files
that meson generates from the public headers. On Linux and Android it is a version script (`local: *`) with
`-Bsymbolic`, so calls inside the library never bind to another HarfBuzz in the process (for example, the system
`libharfbuzz` that GTK loads). On Apple platforms it is an exported symbols list, and on Windows a `.def` file.

### Checks

Each build fails unless:

- the shared library exports exactly the functions in `exports.txt`;
- its dynamic dependencies are in the table above;
- on Android, its `LOAD` segments are aligned to 16 KB;
- `scripts/smoke/smoke.c` compiles against the staged headers and links with the shared library and with the
  static library. The smoke test shapes a string, subsets the font, and draws a glyph with the raster, vector and
  GPU libraries. On Apple platforms it also creates a face with CoreText, and on Windows with DirectWrite.

The smoke test runs when the build machine can run the target: Linux, macOS (x64 under Rosetta), Windows x64, and
WebAssembly under Node. For the other targets it is only linked. `build-info.json` records `ran` or `linked`.

### WebAssembly and Emscripten versions

A WebAssembly static library links only with the Emscripten version that compiled it, so each archive is built once
per version. The workflow builds for each .NET SDK band in its `wasm` matrix, with the Emscripten packs that band's
wasm-tools workload installs (`Microsoft.NET.Workload.Emscripten.Current.Manifest-<band>` on NuGet):

| .NET SDK band | Emscripten |
|---|---|
| 10.0.100 | 3.1.56 |
| 11.0.100 | 6.0.3 |

To add a band, add it to the `wasm` matrix.

The archives are single-threaded, so they link into hosts without WebAssembly threads, such as .NET. Under
Emscripten, meson's `threads` dependency adds `-pthread`, which compiles atomics and shared memory. `build.py` removes
that dependency from HarfBuzz's `meson.build` for the build (and restores the file afterwards) and defines `HB_NO_MT`.
The build fails if an object of `libnharfbuzz.a` uses atomics.

### Known issues in HarfBuzz

- HarfBuzz 15.1.0 does not define `HB_HAS_GPU` in `hb-features.h`, although harfbuzz-gpu is built and exported.
  Later commits fix this. `build-info.json` lists the features as `hb-features.h` defines them.
- With the Windows SDK 10.0.22621, `NAN` is not a constant expression for clang-cl, and HarfBuzz declares
  `static constexpr float INVALID = NAN`. When `build.py` finds this, it force-includes
  `scripts/windows/constexpr-nan.h`, which defines `NAN` as `__builtin_nanf("")`. `build-info.json` then has
  `toolchain.constexprNanShim`.

## Building locally

You need Python 3 and Git, and meson and ninja from `requirements.txt`
(`python3 -m pip install -r requirements.txt`, in a virtual environment if your Python is externally managed).
Linux builds also need Docker, and Android builds an Android NDK. Windows builds need Visual Studio 2022 (C++
tools, and ARM64 tools for arm64) and LLVM in `C:\Program Files\LLVM` (or `HARFBUZZ_BUILDER_LLVM`). WebAssembly
builds run on a Linux x64 host with the toolchain that `fetch-emscripten.py` downloads.

```sh
python3 scripts/fetch-harfbuzz.py --ref 15.1.0 --resolve-only     # the commit and tag a version resolves to
python3 scripts/fetch-harfbuzz.py --ref 15.1.0                    # check out ./harfbuzz
scripts/build-linux.sh x64                                        # Linux, in docker/linux/Dockerfile
python3 scripts/build.py macos arm64                              # macOS or Windows, on that OS
python3 scripts/build.py android arm64 --ndk <ndk>                # or ANDROID_NDK_HOME
python3 scripts/package-apple.py ios --list-slices | while read -r slice arch; do
  python3 scripts/build.py "$slice" "$arch" --no-archive
done
python3 scripts/package-apple.py ios                              # the XCFrameworks and the archive
python3 scripts/fetch-emscripten.py --sdk-band 10.0.100           # toolchains/emscripten-10.0.100
python3 scripts/build.py wasm wasm32 --emscripten toolchains/emscripten-10.0.100
```

`fetch-harfbuzz.py --repository` also takes a git URL or a local path. The meson build is in `build/<platform>`, the
staged files in `artifacts/`, and the archive in `release/`. `build.py --skip-build` packages an existing meson
build again.
