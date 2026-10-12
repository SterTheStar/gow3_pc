#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
mkdir -p out
# Windows: MSYS2 CLANG64 (packages in packaging/windows/README.md). The GPU library is static
# there and CMake links gow3-probe.exe as well; no PGO (GCC-only flags), LTO off by default.
case $(uname -s) in
MINGW*|MSYS*)
    # shellcheck source=msys2-env.sh
    source ./msys2-env.sh
    for tool in clang cmake ninja pkg-config; do
        command -v "$tool" >/dev/null || { echo "Need $tool from MSYS2 CLANG64 (packaging/windows/README.md)." >&2; exit 1; }
    done
    if [[ ! -f gpu/third_party/fsr-vulkan/CMakeLists.txt || ! -f gpu/third_party/imgui/imgui.h ||
          ! -f gpu/third_party/xbyak/xbyak/xbyak.h ]]; then
        git submodule update --init --recursive
    fi
    for patch in gpu/patches/fsr-vulkan/*.patch; do
        if ! git -C gpu/third_party/fsr-vulkan apply --reverse --check "$PWD/$patch" 2>/dev/null; then
            git -C gpu/third_party/fsr-vulkan apply "$PWD/$patch"
        fi
    done
    cmake -S gpu -B out/gpu -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DGOW3_PGO=off \
        -DGOW3_LTO="${GOW3_LTO:-OFF}" -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ >/dev/null
    echo "GPU library and loader: LTO ${GOW3_LTO:-OFF}"
    wjobs=()
    if [[ -n ${GOW3_JOBS:-} ]]; then
        wjobs=(-j "$GOW3_JOBS")
    fi
    if ! ninja -C out/gpu "${wjobs[@]}" gow3-probe gow3-gpu-capabilities gow3-play > out/gpu-build.log 2>&1; then
        grep -v '^\[' out/gpu-build.log | tail -40 >&2
        echo 'Build failed (full log: out/gpu-build.log)' >&2; exit 1
    fi
    cp out/gpu/gow3-probe.exe out/gpu/gow3-gpu-capabilities.exe out/gpu/gow3-play.exe out/
    echo "Built $PWD/out/gow3-probe.exe"
    exit 0
    ;;
esac
if [[ -z ${CC:-} ]]; then
    CC=$(command -v cc || command -v gcc || true)
fi
if [[ -z ${CC:-} ]]; then echo 'Install GCC/Clang or set CC.' >&2; exit 1; fi
# Dependencies come from the system toolchain via pkg-config (Vulkan loader/headers, SDL3).
if ! { command -v pkg-config >/dev/null && pkg-config --exists vulkan sdl3 && command -v cmake >/dev/null && command -v ninja >/dev/null; }; then
    echo 'Need pkg-config with vulkan and sdl3, cmake and ninja installed on the system.' >&2; exit 1
fi
read -r -a includes <<< "$(pkg-config --cflags vulkan sdl3 libavcodec libavutil libswscale)"
read -r -a libraries <<< "$(pkg-config --libs vulkan sdl3 libavcodec libavutil libswscale)"
# GPU library (shadPS4 video core + drivers), built by CMake into out/gpu/libgow3gpu.so.
# GOW3_PGO: generate (instrumented build that writes pgo/ while the game runs), use, off.
# Default: use the profile in pgo/ when there is one. GOW3_LTO=OFF disables link-time optimization.
# GOW3_JOBS=N limits ninja parallelism (default: all cores). Low-memory machines want
# GOW3_JOBS=4 (or less) together with GOW3_LTO=OFF: the default LTO link alone needs
# several GB and each compiler job adds ~0.5-1 GB on top.
ninja_jobs=()
if [[ -n ${GOW3_JOBS:-} ]]; then
    ninja_jobs=(-j "$GOW3_JOBS")
fi
pgo=${GOW3_PGO:-}
if [[ -z $pgo ]]; then
    if [[ -n $(find pgo -name '*.gcda' -print -quit 2>/dev/null) ]]; then pgo=use; else pgo=off; fi
fi
mkdir -p pgo
# Submodules (git clone --recursive, or: git submodule update --init) and this port's changes
# to FSR-Vulkan (gpu/patches/fsr-vulkan), applied to its working tree once. The submodule
# stays marked as modified afterwards by design (same on Windows): do not commit that state,
# it is derived from gpu/patches at build time.
if [[ ! -f gpu/third_party/fsr-vulkan/CMakeLists.txt || ! -f gpu/third_party/imgui/imgui.h || ! -f gpu/third_party/xbyak/xbyak/xbyak.h ]]; then
    git submodule update --init --recursive
fi
for patch in gpu/patches/fsr-vulkan/*.patch; do
    if ! git -C gpu/third_party/fsr-vulkan apply --reverse --check "$PWD/$patch" 2>/dev/null; then
        git -C gpu/third_party/fsr-vulkan apply "$PWD/$patch"
    fi
done
cmake -S gpu -B out/gpu -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DGOW3_PGO="$pgo" \
    -DGOW3_LTO="${GOW3_LTO:-ON}" -DGOW3_PGO_DIR="$PWD/pgo" >/dev/null
echo "GPU library: PGO $pgo, LTO ${GOW3_LTO:-ON}"
# A failed GPU build must stop here: an older libgow3gpu.so would otherwise be used silently.
if ! ninja -C out/gpu "${ninja_jobs[@]}" gow3gpu > out/gpu-build.log 2>&1; then
    grep -v '^\[' out/gpu-build.log | tail -40 >&2
    echo 'GPU library build failed (full log: out/gpu-build.log)' >&2; exit 1
fi
# $ORIGIN/gpu: packaged copies keep the library next to the binary without patching it.
gpu=(-Lout/gpu -lgow3gpu -Wl,-rpath,'$ORIGIN/gpu' -Wl,-rpath,"$PWD/out/gpu" -rdynamic)
runtime=(src/runtime*.c)
# The probe/runtime links below recompile from source on every call; skip them when the last
# build used the same toolchain and flags and nothing changed since (ninja above already
# keeps the GPU library itself fresh). A toolchain switch rebuilds once, then stays incremental.
fingerprint=$(printf '%s|%s|%s|%s' "$CC" "$("$CC" --version | head -n 1)" "$pgo" "${GOW3_LTO:-ON}")
if [[ ${1:-} != --test && -f out/gow3-probe && -f out/.build-fingerprint && $(cat out/.build-fingerprint) == "$fingerprint" ]] \
    && [[ ! out/gpu/libgow3gpu.so -nt out/gow3-probe ]] \
    && [[ ! out/libatrac9.a -nt out/gow3-probe ]] \
    && [[ -z $(find src tools/gpu_capabilities.c third_party/LibAtrac9/C/src gpu/CMakeLists.txt build.sh -newer out/gow3-probe -print -quit 2>/dev/null) ]]; then
    echo "gow3-probe is up to date ($PWD/out/gow3-probe)"
    exit 0
fi
# Third-party decoders: compiled once, without this project's -Werror policy.
atrac9=(third_party/LibAtrac9/C/src/*.c)
if [[ ! -f out/libatrac9.a || -n $(find third_party/LibAtrac9/C/src -newer out/libatrac9.a -name '*.c') ]]; then
    rm -rf out/atrac9 && mkdir -p out/atrac9
    for source in "${atrac9[@]}"; do "$CC" -std=c99 -O2 -g -w -c "$source" -o "out/atrac9/$(basename "${source%.c}").o"; done
    ar rcs out/libatrac9.a out/atrac9/*.o
fi
"$CC" -std=c11 -O2 -g -Wall -Wextra -Werror -pthread -no-pie "${includes[@]}" -I. -Isrc src/probe.c "${runtime[@]}" src/vulkan_smoke.c out/libatrac9.a -lm "${gpu[@]}" "${libraries[@]}" -o out/gow3-probe
printf '%s\n' "$fingerprint" > out/.build-fingerprint
echo "Built $PWD/out/gow3-probe"
# GPU check for run.sh (live_resolution=auto): links only the Vulkan loader.
"$CC" -std=c11 -O2 -Wall -Wextra -Werror tools/gpu_capabilities.c "${libraries[@]}" -o out/gow3-gpu-capabilities
if [[ ${1:-} == --test ]]; then
    "$CC" -std=c11 -O2 -g -Wall -Wextra -Werror -pthread "${includes[@]}" -I. -Isrc tests/test_pad.c "${libraries[@]}" -o out/pad-test
    out/pad-test
    "$CC" -std=c11 -O2 -g -Wall -Wextra -Werror -pthread -I. -Isrc tests/test_runtime.c "${runtime[@]}" out/libatrac9.a -lm "${gpu[@]}" "${libraries[@]}" -o out/runtime-test
    out/runtime-test
    "$CC" -std=c11 -O2 -g -Wall -Wextra -Werror -pthread -Isrc tests/test_file_mods.c -o out/file-mods-test
    out/file-mods-test
    "$CC" -std=c11 -O2 -g -Wall -Wextra -Werror -pthread -I. -Isrc tests/test_sema.c "${runtime[@]}" out/libatrac9.a -lm "${gpu[@]}" "${libraries[@]}" -o out/sema-test
    out/sema-test
    "$CC" -std=c11 -D_GNU_SOURCE -O2 -g -Wall -Wextra -Werror -I. -Isrc tests/test_content.c src/runtime_content.c -o out/content-test
    out/content-test
    "$CC" -std=c11 -O2 -g -Wall -Wextra -Werror -pthread "${includes[@]}" -I. -Isrc tests/test_mp3.c src/runtime_mp3.c out/libatrac9.a "${libraries[@]}" -o out/mp3-test
    out/mp3-test tests/data/sine.mp3
    "$CC" -std=c11 -O2 -g -Wall -Wextra -Werror -pthread "${includes[@]}" -I. -Isrc tests/test_videodec.c "${libraries[@]}" -o out/videodec-test
    out/videodec-test tests/data/tiny.h264
    "$CC" -std=c11 -O2 -g -Wall -Wextra -Werror -pthread -I. -Isrc tests/test_services.c -o out/services-test
    out/services-test
fi
