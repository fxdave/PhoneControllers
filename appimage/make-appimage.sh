#!/bin/sh
# Builds dist/Phone_Controllers-<version>-<arch>.AppImage in the Arch Linux build container,
# after get-dependencies.sh and the anylinux-setup-action (which provides quick-sharun).
set -eu

APP_ID=io.github.fxdave.PhoneControllers
ARCH=$(uname -m)
VERSION=${VERSION:-$(git describe --tags --always 2>/dev/null || echo dev)}
VERSION=${VERSION#v}
export ARCH VERSION
export OUTPATH=./dist
export DESKTOP=/usr/share/applications/$APP_ID.desktop
export ICON=/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg
export DEPLOY_PYTHON=1
export DEPLOY_GTK=1 GTK_DIR=gtk-4.0
export STARTUPWMCLASS=$APP_ID
export GTK_CLASS_FIX=1

# Install the app system-wide first so quick-sharun can trace it like a packaged app.
install -d /usr/share/phone-controllers
cp -r phonecontrollers static games /usr/share/phone-controllers/
install -Dm755 appimage/phone-controllers /usr/bin/phone-controllers
install -Dm644 data/$APP_ID.desktop -t /usr/share/applications
install -Dm644 data/$APP_ID.metainfo.xml -t /usr/share/metainfo
install -Dm644 data/icons/$APP_ID.svg -t /usr/share/icons/hicolor/scalable/apps

PYDIR=$(python3 -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')

# libadwaita and GTK are loaded by PyGObject at run time, ssl is needed for the HTTPS server
# (quick-sharun leaves it out unless asked), openssl creates the certificate, and the
# compiled Python modules (Pillow, evdev, aiohttp...) need their libraries traced too. Only
# modules from python-* packages are taken: the build image has others, like boost's MPI
# bindings, whose libraries aren't installed and which would abort the deployment.
# shellcheck disable=SC2046
quick-sharun \
	/usr/bin/phone-controllers \
	/usr/bin/openssl \
	/usr/lib/libadwaita-1.so* \
	/usr/lib/libgtk-4.so* \
	"$PYDIR"/lib-dynload/_ssl.*.so \
	"$PYDIR"/lib-dynload/_hashlib.*.so \
	$(pacman -Qlq $(pacman -Qq | grep '^python-') | grep "^$PYDIR/site-packages/.*\.so$") \
	/usr/share/phone-controllers

mkdir -p ./AppDir/share/metainfo
cp -v /usr/share/metainfo/$APP_ID.metainfo.xml ./AppDir/share/metainfo/

quick-sharun --make-appimage
