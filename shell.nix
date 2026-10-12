# Build environment: `nix-shell native_probe/shell.nix --run 'bash native_probe/run.sh'`
{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  packages = with pkgs; [
    gcc gnumake cmake ninja pkg-config python3 binutils
    vulkan-headers vulkan-loader mesa sdl3
    # GPU library (gpu/): shadPS4 video core dependencies
    ffmpeg-headless boost fmt magic-enum robin-map xxhash vulkan-memory-allocator glslang spirv-cross xbyak zydis spirv-headers miniz libx11 libxcb xorgproto wayland
    # Linux launcher (launcher/gow3_launcher_linux.py): GTK4/libadwaita through PyGObject
    gtk4 libadwaita gobject-introspection
    (python3.withPackages (ps: [ ps.pygobject3 ]))
  ];
  # Mesa ICDs for run.sh (GOW3_BUNDLED_VK_DRIVER_FILES): the loader alone finds no GPU
  # inside nix-shell; gow3_vulkan.py adds the host NVIDIA driver on top when present.
  shellHook = ''
    export GOW3_BUNDLED_VK_DRIVER_FILES="${pkgs.mesa}/share/vulkan/icd.d/radeon_icd.x86_64.json:${pkgs.mesa}/share/vulkan/icd.d/intel_icd.x86_64.json"
  '';
}
