---
title: Manual stable desktop release
description: Build, sign, test, and publish stable desktop assets without a tag-triggered cloud build.
---

# Manual stable desktop release

Stable desktop packages are built and signed on their native build hosts. Pushing
`v*` tags does not start a cloud build. The only automatic stable-release action
starts after a maintainer publishes a GitHub Release: it validates the required
Portable assets only.

Run `scripts/build-desktop-release.ps1` once on each target host. It signs the
Portable manifests, stages the resulting assets under `release-assets/<version>/`,
and never creates a tag, GitHub Release, upload, or update-service request.

Before running it, build the matching Nuitka backend on the native host and put
it in the adjacent `N.E.K.O.-PC/bin` directory (`projectneko_server.exe` on
Windows, `projectneko_server` on macOS/Linux). The script packages that backend
with the locally available Electron signing identity.

Prepare the Nuitka launcher with
`uv run python scripts/prepare_nuitka_plugins.py prepare --build-commit` before
compiling it. From the same N.E.K.O. checkout, run
`uv run python scripts/build_plugin_tools.py --backend <backend-executable>`
using the same Python used for the Nuitka build. On macOS, pass the executable
at `projectneko_server.app/Contents/MacOS/projectneko_server` and stage the tools
before signing the backend bundle. Keep both the bundle and its outer
`bin/projectneko_server` launcher when copying the backend to N.E.K.O.-PC.
For local PyInstaller builds, `specs/launcher.spec` generates the same worker
entry point. Stage the tool resources beside the executable from that checkout.

Before signing the Portable manifests, the release script checks the tool
resources and the executable's build commit, then starts the backend extracted
from the full Portable ZIP or tar.gz in a temporary profile. It loads the plugin
manager and uses its HTTP endpoints to create a project, update workflows,
create its repository, run precheck, and exercise all three publication modes.
On Linux it also checks the resources and build commit inside the AppImage.
Acceptance uses an isolated GitHub CLI fixture, a temporary Git repository,
and local GitHub and Market test APIs. It cancels and retries a Market
notification, then restarts the backend and reads the publication receipt.
The build pins generated plugin workflows to the build commit when GitHub
confirms that it is on the official main branch. Other builds use official
`main` for their generated workflows.

The publishing script also reuses the trusted manifest verifier from the sibling
`N.E.K.O.-Launcher/src/portable-manifest.js` checkout. It also supports the
`launcher` checkout and the existing `N.E.K.O.-PC/src/main/portable-update.js`
layout. For a custom layout, pass the full verifier path with `-ManifestVerifierPath`.

```powershell
./scripts/build-desktop-release.ps1 `
  -Version 0.8.4 `
  -ManifestSigningKeyPath D:\secure\portable-manifest-ed25519.pem `
  -PreviousReleaseTag v0.8.3
```

For macOS, run PowerShell on each architecture and select it explicitly:

```powershell
./scripts/build-desktop-release.ps1 -Version 0.8.4 `
  -Platform macos -Architecture arm64 `
  -ManifestSigningKeyPath /secure/portable-manifest-ed25519.pem `
  -PreviousReleaseTag v0.8.3
```

The `-PreviousReleaseTag` option is optional. When supplied, the script reads
the prior manifest through `gh release download` and creates a differential
package when it is smaller than the full package. It never calls `gh release
create` or `gh release upload`.

Test every staged package before creating a non-prerelease GitHub Release. A
published stable release must include these Portable full packages, manifests,
and manifest `.sig` files for Windows x64, macOS x64/arm64, Linux x64, and Linux
x64 AppImage.

Publishing the Release runs GitHub-side asset validation only; it does not call
N.E.K.O.-Update. After every native build has been collected under
`release-assets/<version>/`, run the following on the local release host:

```powershell
$env:NEKO_UPDATE_ADMIN_TOKEN = '<secret>'
.\scripts\publish-desktop-release-assets.ps1 `
  -Tag 'v0.8.4' `
  -OssReleaseRoot 'oss://<local-bucket>/releases' `
  -CdnBaseUrl 'https://download.project-neko.cn' `
  -ServiceUrl 'https://update.project-neko.cn'
```

The script supports stable releases only. It uploads the already staged build
artifacts directly and never downloads them from GitHub. Before upload it verifies
that staged filenames exactly match the published Release and that every Portable
manifest has its matching `.sig` file. Existing immutable OSS objects are never
overwritten: a rerun is allowed only when their SHA-256 matches the staged asset.
Before registering the `aliyun` mirror, the script downloads every CDN asset and
compares its SHA-256 with the staged asset. OSS credentials, endpoints, and Bucket
names stay exclusively in the local ossutil configuration and are never added to
GitHub Actions, repository variables, or repository files.
