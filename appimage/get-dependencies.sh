#!/bin/sh
# Installs what the AppImage bundles, inside the Arch Linux build container.
set -eu

pacman -Syu --noconfirm --needed \
	python \
	python-gobject \
	python-aiohttp \
	python-evdev \
	python-qrcode \
	python-pillow \
	gtk4 \
	libadwaita \
	openssl

# Smaller builds of common libraries (mesa, llvm and friends).
get-debloated-pkgs --add-common --prefer-nano
