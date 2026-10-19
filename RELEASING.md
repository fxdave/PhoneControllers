# Releasing

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

## Building from GitHub

`flatpak/io.github.fxdave.PhoneControllers.github.yml` builds the `main` branch from GitHub
instead of the working copy, so it also works in a sandboxed build (the command is at its
top). The Flathub linter flags its filename and its branch source; both are Flathub
submission rules, and this manifest isn't for Flathub.

## Flathub

The app isn't on Flathub. A submission has to follow Flathub's
[requirements](https://docs.flathub.org/docs/for-app-authors/requirements), and two of them
matter here:

- **Generative AI policy.** AI-generated code has to be disclosed, and the manifest submitted
  to Flathub must not contain AI-generated or AI-assisted content. AI tools or agents must not
  open or automate the submission pull request, or write its description, commit messages or
  review replies. The manifest in `flatpak/` was written with an AI assistant, so it is only
  for local builds; a Flathub submission needs one written by hand, and a person files and
  answers the pull request.
- **Development history.** Flathub expects a sustained commit history, real-world use and a
  commitment to maintenance; very young projects are generally not accepted.

The Flathub linter's screenshot and icon checks only pass in a build made with Flathub's
mirroring options (`--compose-url-policy=full
--mirror-screenshots-url=https://dl.flathub.org/media/`), so a plain local build always
reports them.
