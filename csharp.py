"""Compile C# Program.cs files in throw-away sandbox projects and parse the compiler errors."""

import os                                               # Environment variables for the dotnet CLI
import queue                                            # A thread-safe list of free sandboxes
import re                                               # Parse compiler messages
import subprocess                                       # Run "dotnet" commands
import time                                             # Measure build time
from concurrent.futures import ThreadPoolExecutor       # Run several builds at the same time
from dataclasses import dataclass, field                # A small class to hold a build result
from pathlib import Path                                # File paths that work on any OS

from tqdm import tqdm                                   # Progress bar

ROOT = Path(__file__).resolve().parent                  # The project folder (wherever it is on this machine)
SANDBOX_DIR = ROOT / "sandbox"                          # Sandboxes live in sandbox\s0, sandbox\s1, ...
TARGET_FRAMEWORK = "net10.0"                            # Compile against .NET 10
BUILD_TIMEOUT = 180                                     # Seconds before a build is considered stuck

DOTNET_ENV = {**os.environ,                             # Copy the normal environment, plus:
              "DOTNET_CLI_UI_LANGUAGE": "en",           # English compiler messages on every PC (our regex expects English)
              "DOTNET_NOLOGO": "1",                     # No welcome banner
              "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}       # No telemetry

DIAGNOSTIC = re.compile(                                # One compiler message, e.g. Program.cs(12,5): error CS1002: ; expected
    r"^(?P<file>.+?\.cs)\((?P<line>\d+),(?P<col>\d+)\): (?P<severity>error|warning) "
    r"(?P<code>[A-Z]+\d+): (?P<message>.+?)(?: \[[^\]]+\])?$", re.MULTILINE)


@dataclass
class BuildResult:
    succeeded: bool                                     # Did it compile?
    errors: list = field(default_factory=list)          # [{"line": 12, "code": "CS1002", "message": "; expected"}, ...]
    seconds: float = 0.0                                # How long the build took

    def error_text(self, code, max_errors=5):
        """Errors in the format the model will see: message plus the offending source line."""
        lines = code.splitlines()                       # The source code, line by line
        out = []
        for e in self.errors[:max_errors]:              # At most 5 errors (keeps the model input short)
            src = lines[e["line"] - 1].strip() if 0 < e["line"] <= len(lines) else ""   # The line the error points at
            out.append(f"Line {e['line']}: error {e['code']}: {e['message']}\n    > {src}")
        return "\n".join(out)


def ensure_sandbox(name):
    """Create sandbox\\<name> as a 'dotnet new web' project the first time; reuse it afterwards."""
    path = SANDBOX_DIR / name
    if not (path / "Sandbox.csproj").exists():          # Only the first time
        subprocess.run(["dotnet", "new", "web", "-n", "Sandbox", "-o", str(path),
                        "--framework", TARGET_FRAMEWORK, "--force"],
                       check=True, capture_output=True, env=DOTNET_ENV)     # Create the project
        subprocess.run(["dotnet", "restore", str(path)],
                       check=True, capture_output=True, env=DOTNET_ENV)     # Download its references once
    return path


def compile_code(code, sandbox="s0"):
    """Write code into the sandbox's Program.cs, build it, and return a BuildResult."""
    path = ensure_sandbox(sandbox)
    (path / "Program.cs").write_text(code, encoding="utf-8")   # Replace Program.cs with the code to test
    start = time.time()
    try:
        proc = subprocess.run(["dotnet", "build", str(path), "--no-restore", "-nologo", "-v", "q", "-clp:NoSummary"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=BUILD_TIMEOUT, env=DOTNET_ENV)
    except subprocess.TimeoutExpired:                   # A stuck build counts as a failure
        return BuildResult(False, [{"line": 0, "code": "TIMEOUT", "message": "build timed out"}], BUILD_TIMEOUT)
    errors, seen = [], set()                            # Errors in order, without duplicates
    for m in DIAGNOSTIC.finditer(proc.stdout):          # Every compiler message
        if m["severity"] != "error":                    # Ignore warnings
            continue
        key = (int(m["line"]), m["code"], m["message"]) # The compiler can print the same error twice
        if key not in seen:
            seen.add(key)
            errors.append({"line": int(m["line"]), "code": m["code"], "message": m["message"]})
    ok = proc.returncode == 0                           # 0 = build succeeded
    if not ok and not errors:                           # Failed, but not with a normal C# error (e.g. a project error)
        errors = [{"line": 0, "code": "OTHER", "message": proc.stdout.strip()[-300:]}]
    return BuildResult(ok, errors, round(time.time() - start, 1))


def compile_many(codes, workers=4):
    """Compile many programs in parallel, each worker in its own sandbox. Returns results in the same order."""
    free = queue.Queue()                                # Sandboxes that are not in use right now
    for w in range(workers):
        ensure_sandbox(f"s{w}")                         # Create sandboxes s0..s3 (first time only)
        free.put(f"s{w}")

    def job(code):
        name = free.get()                               # Take a free sandbox (waits if all are busy)
        try:
            return compile_code(code, name)
        finally:
            free.put(name)                              # Give it back, even if something went wrong

    with ThreadPoolExecutor(max_workers=workers) as pool:   # Run "workers" builds at the same time
        return list(tqdm(pool.map(job, codes), total=len(codes)))   # Results in input order, with a progress bar