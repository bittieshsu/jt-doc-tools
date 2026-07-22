# Windows GUI installer (NSIS)

This folder contains the **double-click Windows installer** for jt-doc-tools.
It is fully **separate** from the command-line `install.ps1` / `install.sh`
one-liners — those are untouched and keep working as before. The GUI
installer is an additional, isolated path for Windows desktop users.

## Files

| File | Role |
|---|---|
| `installer.nsi` | NSIS (MUI2) script — wizard, components page, registry, uninstaller |
| `install_core.ps1` | Standalone, non-interactive install logic invoked by the wizard |
| `uninstall_core.ps1` | Standalone uninstall logic invoked by the NSIS uninstaller |
| `assets/jtdt.ico` | Multi-resolution app icon (generated from `docs/favicon-512.png`) |
| `winsw.exe` | Bundled Windows service wrapper (shared with `install.ps1`) |

The installer is a **thin bootstrapper**: the `.exe` ships only the two
PowerShell scripts + LICENSE + icon (well under 1 MB). At runtime
`install_core.ps1` downloads uv-managed Python, clones the repo, runs
`uv sync`, and registers the WinSW service — exactly the same end state as
the one-liner, just driven from a GUI.

## Components (all default-checked)

| Component | Switch passed to `install_core.ps1` | Notes |
|---|---|---|
| Core (required) | — | program + Python venv |
| OCR engine | `-InstallOcr` | VC++ redist + tesseract chi_tra (~700 MB of deps via uv) |
| Office engine | `-InstallOffice` | OxOffice silent MSI, LibreOffice fallback (~600 MB) |
| Windows service | `-InstallService` | WinSW autostart service |
| LAN access | `-InstallFirewall` | binds `0.0.0.0` + firewall inbound rule; otherwise localhost-only |

## Building locally

Requires NSIS (`makensis`). On Debian/Ubuntu: `sudo apt-get install nsis`.

```bash
cd packaging/windows
makensis -DVERSION=1.11.68 installer.nsi
# -> jt-doc-tools-1.11.68-setup.exe
```

`VERSION` defaults to `0.0.0` if not supplied. The LICENSE shown in the
wizard is read from `../../LICENSE`.

## CI build

`.github/workflows/release-windows-installer.yml` builds on an **Ubuntu**
runner (cheaper + faster than a Windows runner for makensis):

- Push a `v*` tag → builds and **attaches the `.exe` to the GitHub Release**.
- Manual `workflow_dispatch` → builds and uploads as a **workflow artifact**
  (use this to validate the build without cutting a release).

## Code signing (SignPath OSS — production certificate issued 2026-07)

The Windows installer is code-signed through the **SignPath Foundation**'s
free code-signing programme for open-source projects (production/release
certificate issued 2026-07); the signing infrastructure is provided by
**SignPath.io**. Attribution is stated on the download page (README + the
GitHub Pages site) per the SignPath Foundation terms
(<https://signpath.org/terms.html>).

CI wiring (`.github/workflows/release-windows-installer.yml`):

1. Repo secrets: `SIGNPATH_API_TOKEN`, `SIGNPATH_ORG_ID` (the signing step is
   gated on `SIGNPATH_ORG_ID` — auto-skips when absent, so builds still work
   before secrets are added).
2. `env:` slugs — `SIGNPATH_PROJECT_SLUG=jt-doc-tools`,
   `SIGNPATH_ARTIFACT_SLUG=initial`, `SIGNPATH_POLICY_SLUG=release-signing`
   (was `test-signing` during the test-certificate phase). Confirm the policy
   slug matches the one in your SignPath console.
3. Two-phase NSIS build (sign `uninstall.exe` first, then embed + sign the
   installer): <https://about.signpath.io/documentation/build-system-integration/nsis>

SmartScreen reputation still accumulates over ~30 days / 1000+ downloads
even when signed; the SignPath countersignature shows "Verified by
SignPath" in the *More info* dialog meanwhile.

## Uninstall design

Two uninstall entry points, both verified on a real Windows machine:

- **Interactive** (Control Panel → Uninstall, or the Start-menu shortcut):
  `UninstallString` = `"uninstall.exe"`. NSIS's standard auto-relaunch-to-temp
  shows the wizard and removes everything.
- **Silent** (`QuietUninstallString` = `"uninstall.exe" /S _?=$INSTDIR`):
  runs **in-place** (`_?=`). This matters because NSIS's auto-relaunch does
  **not** forward `/S`, so a plain `"uninstall.exe" /S` would relaunch into an
  interactive copy and do nothing. In-place mode runs the uninstall section
  directly; because the running `uninstall.exe` can't delete itself, the
  section schedules a **detached `cmd` cleanup** (`ping` delay + `rmdir`) to
  remove the leftover folder after the process exits.

`uninstall_core.ps1` (stop service → WinSW uninstall → kill any port-8765
orphan → remove firewall rule → strip PATH → optional data purge) runs first;
then NSIS removes program files, shortcuts and the registry key. User data in
`%ProgramData%\jt-doc-tools` is **kept by default** (the wizard / `/SD IDNO`
prompt), so a reinstall reuses bank accounts, signatures and history.

> Testing note: when validating the silent path over SSH, always use
> `Start-Process ... -Wait`. Without `-Wait` the SSH session closes and Windows
> tears down the detached child, which looks like "the uninstaller did nothing"
> but is purely a headless-test artifact.

## x64 and ARM64 Windows

The installer is built `x86-unicode` (NSIS installers are always 32-bit) but
installs the **64-bit** product and works on both **x64** and **ARM64**
Windows. Because the installer process is 32-bit (WOW64), two gotchas were
handled — both verified on a real Windows machine:

- **64-bit PowerShell via Sysnative.** `installer.nsi` invokes
  `$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe` (with a fallback
  to plain `powershell.exe`) so the install/uninstall core runs in the
  **native** registry + filesystem view. The 32-bit `powershell.exe` would
  read `WOW6432Node` and miss things like the x64 VC++ Redistributable.
- **`$env:ProgramFiles` is the x86 path under WOW64.** Even the 64-bit child
  inherits `%ProgramFiles%`=`C:\Program Files (x86)` from the 32-bit parent,
  so `install_core.ps1` uses **`$env:ProgramW6432`** (always the real 64-bit
  Program Files) for Office detection. Otherwise an existing LibreOffice in
  `C:\Program Files\` is not found and a needless winget reinstall is kicked off.
- **System-level shortcuts.** `SetShellVarContext all` puts Start-menu
  shortcuts in the All-Users menu, not the elevating user's profile.

On **ARM64**, x64/x86 binaries run under emulation, so the installer and uv
all work. Note `EasyOCR` (PyTorch) wheels may be unavailable on Windows
ARM64; OCR then falls back to tesseract (the installer logs a warning).

## Relationship to the other install paths

```
curl ... | iex        -> install.ps1        (CLI / advanced Windows users)   UNCHANGED
curl ... | sh         -> install.sh         (Linux / macOS users)            UNCHANGED
double-click .exe     -> installer.nsi -> install_core.ps1   (Windows GUI)   NEW, isolated
```
