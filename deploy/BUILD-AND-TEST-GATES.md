# Building RigPilot releases

Run commands from the repository root. For a source installation or an initial connection to a controller, start with the [project guide](../README.md). The following steps are for creating distribution archives.

Before building on Linux or macOS, run `sh scripts/prepare-unix-modes.sh` from the repository root. It restores the executable bits for the build scripts, launchers and image files.

## Controller

The offline controller archive targets CPython 3.10 on Linux x86_64. On an Ubuntu 22.04 amd64 build host with that interpreter, restore the runtime dependencies into a new or empty wheel directory before building. The lock file pins the SHA-256 digest of each accepted wheel. Keep the wheel directory outside version control.

```sh
python3.10 -m pip download --require-hashes --only-binary=:all: \
  -r deploy/controller-runtime/requirements-controller-linux-py310.lock \
  -d deploy/controller-runtime/wheelhouse/linux-x86_64-py310
bash deploy/build-all-releases.sh --skip-linux
```

The builder checks the wheel names and digests, adds the controller payload and writes the archive and checksum to `outputs/`. The [controller HTTP checks](CONTROLLER-HTTP-SMOKE.md) exercise the packaged application using a separate database and simulated devices.

## Linux Agent

Build the x86_64 Agent on Ubuntu 22.04 amd64 with glibc 2.35 and a Python installation with `venv` support. The builder installs its pinned dependencies, runs the Agent suite and validates both archive layouts. Use a new output directory for a repeated build of the same version.

```sh
bash deploy/linux/build-ubuntu-portable.sh --check-build-host
bash deploy/build-all-releases.sh --skip-controller
```

The full archive contains the systemd installer. The online-update archive contains the frozen Agent and a release manifest. See [Ubuntu Agent packaging](linux/README.md).

## Windows Agent

Use Windows, a `win-amd64` Python interpreter and the .NET 8 SDK. In PowerShell at the repository root, pass the path to your x64 Python executable.

```powershell
.\deploy\windows\build-windows-portable.ps1 -PythonPath (Get-Command python.exe).Source
```

The builder runs the Windows contract tests, checks the frozen executable and verifies the contents of the full ZIP. See the [Windows packaging guide](windows/README-Windows.md).
