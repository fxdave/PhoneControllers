# NixOS has no /usr/bin/python and a pip venv can't find GTK, so run from source with:
#   nix-shell --run "python -m phonecontrollers"
#   nix-shell --run "python -m pytest"
{ pkgs ? import <nixpkgs> { } }:

pkgs.mkShell {
  packages = [
    (pkgs.python3.withPackages (python: [
      python.aiohttp
      python.evdev
      python.qrcode
      python.pillow
      python.pygobject3
      python.pytest
    ]))
    pkgs.gtk4
    pkgs.libadwaita
    pkgs.gobject-introspection
    pkgs.openssl
    pkgs.luajit
    pkgs.nodejs
    pkgs.wrapGAppsHook4
  ];
}
