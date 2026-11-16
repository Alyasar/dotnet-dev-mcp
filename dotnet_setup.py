"""Make sure the .NET SDK is available. On Linux hosts without it (Streamlit Cloud), install it on first start."""

import os                                               # os: environment variables (PATH)
import shutil                                           # shutil.which: is a command already installed?
import subprocess                                       # subprocess: run the install script
import urllib.request                                   # urllib: download the install script
from pathlib import Path                                # Path: folders and file paths

DOTNET_DIR = Path.home() / ".dotnet"                    # Where we install the SDK (the app's home folder, always writable)
CHANNEL = "10.0"                                        # .NET 10, the same version the eval used
INSTALL_SCRIPT = "https://dot.net/v1/dotnet-install.sh" # Microsoft's official install script


def _use_local_install():                               # Put our installed SDK first on PATH, for this process and its builds
    os.environ["DOTNET_ROOT"] = str(DOTNET_DIR)
    os.environ["PATH"] = f"{DOTNET_DIR}{os.pathsep}{os.environ['PATH']}"
    os.environ["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"   # Don't require the ICU library (not installed on the server)


def ensure_dotnet():
    """Returns where dotnet came from: 'system' (already installed, e.g. your PC), 'cached' or 'installed'."""
    os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")   # No telemetry
    os.environ.setdefault("DOTNET_NOLOGO", "1")                 # No welcome banner
    os.environ.setdefault("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1")

    if (DOTNET_DIR / "dotnet").exists():                # Installed by an earlier start of this server
        _use_local_install()
        return "cached"
    if shutil.which("dotnet"):                          # Already on this computer (your Windows PC)
        return "system"
    if os.name == "nt":                                 # Windows without .NET: ask the user to install it
        raise RuntimeError("The .NET 10 SDK is not installed. Get it from https://dotnet.microsoft.com/download")

    DOTNET_DIR.mkdir(parents=True, exist_ok=True)
    script = DOTNET_DIR.parent / "dotnet-install.sh"
    urllib.request.urlretrieve(INSTALL_SCRIPT, script)  # Download the script
    r = subprocess.run(["bash", str(script), "--channel", CHANNEL, "--install-dir", str(DOTNET_DIR)],
                       capture_output=True, text=True, timeout=600)   # Install the SDK (about 1 minute)
    if r.returncode != 0:
        raise RuntimeError("Installing .NET failed:\n" + (r.stdout + r.stderr)[-1000:])
    _use_local_install()
    return "installed"