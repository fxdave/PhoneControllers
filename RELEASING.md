# Releasing

Phone Controllers ships as a Flatpak on Flathub under the ID
`io.github.fxdave.PhoneControllers`.

## Every release

1. Add a `<release>` entry at the top of `<releases>` in
   `data/io.github.fxdave.PhoneControllers.metainfo.xml`, with the version, date and a short
   description of what changed.
2. If the Python dependencies changed, regenerate the pinned list:
   `.venv/bin/python flatpak/generate-python-deps.py`.
3. Check the source and the metadata:

   ```sh
   .venv/bin/python -m pytest
   flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest flatpak/io.github.fxdave.PhoneControllers.yml
   flatpak run --command=flatpak-builder-lint org.flatpak.Builder appstream data/io.github.fxdave.PhoneControllers.metainfo.xml
   ```

4. Commit, tag and push: `git tag v0.1.0 && git push origin main v0.1.0`.
5. Write the Flathub manifest for the tag:
   `python3 flatpak/make-flathub-manifest.py v0.1.0 ../flathub-PhoneControllers`.
   It points the app's source at the tag and copies `python-deps.json` next to it.
6. Build and lint it the way Flathub does. The screenshot and icon checks only pass with
   Flathub's mirroring options, so a plain local build always fails them:

   ```sh
   cd ../flathub-PhoneControllers
   flatpak run --filesystem=$PWD org.flatpak.Builder --force-clean --sandbox --user \
       --install-deps-from=flathub --compose-url-policy=full \
       --mirror-screenshots-url=https://dl.flathub.org/media/ --repo=repo \
       builddir io.github.fxdave.PhoneControllers.yml
   flatpak run --filesystem=$PWD --command=flatpak-builder-lint org.flatpak.Builder builddir builddir
   flatpak run --filesystem=$PWD --command=flatpak-builder-lint org.flatpak.Builder repo repo
   ```

   If the build fails with a FUSE permission error, add `--disable-rofiles-fuse`.

## First submission

Follow [Flathub's submission guide](https://docs.flathub.org/docs/for-app-authors/submission):

1. Fork <https://github.com/flathub/flathub> and branch from `new-pr`.
2. Add the two files from step 5 at the top of the branch and open a pull request against
   `new-pr`, titled `Add io.github.fxdave.PhoneControllers`.
3. Explain the permissions in the pull request:
   - `--device=all`: the app creates its virtual wheel through `/dev/uinput`, which no
     narrower device permission covers, and it watches `/dev/input` to see which game opened
     the wheel.
   - `--share=network`: phones connect to the app's web server on the local network.
   - `--talk-name=org.freedesktop.ScreenSaver`: phones aren't keyboard or mouse input, so
     the app asks the idle daemon (hypridle, KDE) not to blank the screen or suspend while a
     phone is connected. The portal's Inhibit covers GNOME and KDE. (Flathub doesn't allow
     talking to logind, so the Flatpak skips the logind idle inhibitor the unpackaged app
     also takes.)
   - `--filesystem=~/.rigsofrods/config:create`: one-click Rigs of Rods setup (input map and
     force feedback settings).

After the app is accepted, Flathub creates `flathub/io.github.fxdave.PhoneControllers`. Later
releases are pull requests there with the files from step 5.

## Screenshots

The README and the metainfo use the images in `docs/screenshots/`. The metainfo links them
from the `main` branch on GitHub, so they must be pushed before Flathub builds the release.
