"""MCP server: .NET developer tools for AI assistants (compile C#, find namespaces, search docs, fix errors)."""

import os                                               # os.chdir: set the working folder
os.chdir(os.path.dirname(os.path.abspath(__file__)))    # Always run from the project folder, whoever starts the server
ENABLE_FIXER = os.environ.get("ENABLE_FIXER", "1") == "1"   # Set ENABLE_FIXER=0 to leave out the 1 GB fixer model (e.g. on small cloud hosts)
import shutil                                           # shutil.which: find the dotnet command
import subprocess                                       # subprocess: run dotnet
import threading                                        # threading.Lock: one build at a time
import contextlib                                       # redirect_stdout: keep library prints off the MCP channel
import sys                                              # sys.stderr: where those prints go instead
import logging                                          # Python's logging system
logging.getLogger("httpx").setLevel(logging.WARNING)    # Hide the per-request INFO lines from Hugging Face downloads
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)   # Hide the Hugging Face Hub INFO messages
from typing import TypedDict                            # TypedDict: describes the shape of a tool's structured result

from mcp.server.mcpserver import MCPServer              # MCPServer: the SDK's high-level server (called FastMCP in v1)

import api_index                                        # Type / namespace / member lookups (data/api_index.json)
from csharp import compile_code                         # P4's build tool: writes Program.cs into a sandbox and runs dotnet build

@contextlib.asynccontextmanager
async def lifespan(server):                             # Runs once when the server starts serving (stdout is already set up)
    threading.Thread(target=_get_rag, daemon=True).start()   # Load the docs search models in the background
    if ENABLE_FIXER:                                    # Only load the fixer model when the tool is enabled
        threading.Thread(target=_get_llm, daemon=True).start()   # Load the P4 fixer model in the background
    yield {}                                            # The server runs while we're "inside" this block

mcp = MCPServer("dotnet-dev-tools", version="0.5.0", lifespan=lifespan)   # lifespan: start background loading safely

MAX_CODE_CHARS = 20_000                                 # Input guard: longer code is rejected
MAX_ERRORS = 10                                         # Return at most this many errors (the first ones matter most)
MAX_NAME_CHARS = 200                                    # Input guard for name lookups
_build_lock = threading.Lock()                          # All builds share one sandbox, so only one may run at a time


# ---------------- Result shapes (become the tools' output schemas) ----------------
class CompileError(TypedDict):                          # One compiler error, as the client will see it
    line: int                                           # Line number in Program.cs (0 = not tied to a line)
    code: str                                           # Compiler code, e.g. CS1002
    message: str                                        # Compiler message, e.g. "; expected"
    source: str                                         # The offending source line (helps the model locate it)


class BuildReport(TypedDict):                           # The whole result of compile_csharp
    succeeded: bool                                     # Did it compile?
    error_count: int                                    # Total number of errors (may be more than the list shows)
    errors: list[CompileError]                          # The first MAX_ERRORS errors
    seconds: float                                      # Build time


class NamespaceMatch(TypedDict):                        # One place a name was found
    kind: str                                           # "type" or "extension method"
    name: str                                           # The exact name found (e.g. FromFormAttribute for "FromForm")
    namespace: str                                      # e.g. Microsoft.AspNetCore.Mvc
    using: str                                          # The directive to add, e.g. "using Microsoft.AspNetCore.Mvc;"
    already_implicit: bool                              # True if web projects already import this namespace
    detail: str                                         # Assembly, or the static class of an extension method


class NamespaceResult(TypedDict):                       # The result of find_namespace
    query: str                                          # The cleaned-up name that was looked up
    matches: list[NamespaceMatch]                       # Where it exists (empty if nowhere)
    suggestions: list[str]                              # Close names if nothing matched (typos, invented APIs)
    note: str                                           # A one-line interpretation for the model
    sdk: str                                            # The ASP.NET Core version the index was built from


class MembersResult(TypedDict):                         # The result of list_members
    type: str                                           # Full type name ("" if not found or ambiguous)
    members: list[str]                                  # Public method, property and field names
    candidates: list[str]                               # Full names to choose from, or close matches
    note: str                                           # A one-line interpretation for the model


def _report_error(message):                             # A BuildReport for bad input (nothing was compiled)
    return {"succeeded": False, "error_count": 1, "seconds": 0.0,
            "errors": [{"line": 0, "code": "INPUT", "message": message, "source": ""}]}


# ---------------- Docs search (P2's hybrid search + reranker, loaded once in the background) ----------------
DOCS_DEFAULT_K, DOCS_MAX_K = 4, 8                       # Passages to return: default and maximum
_rag = None                                             # The search pipeline, once loaded
_rag_lock = threading.Lock()                            # Only one thread may load it


def _get_rag():
    """Load the docs search pipeline once. Anything it prints goes to stderr (stdout is the MCP channel)."""
    global _rag
    with _rag_lock:                                     # A second caller waits here until loading is done
        if _rag is None:
            with contextlib.redirect_stdout(sys.stderr):   # Protect the stdio protocol from stray prints
                from rag import RagPipeline             # Imported here: it loads PyTorch and models (slow)
                _rag = RagPipeline(provider="ollama")   # Retrieval only: no LLM is called
    return _rag


class DocHit(TypedDict):                                # One passage from the docs
    rank: int                                           # 1 = best match
    section: str                                        # "Article > Section" header
    url: str                                            # Link to the official docs page
    text: str                                           # The passage itself


class DocsResult(TypedDict):                            # The result of search_aspnetcore_docs
    query: str
    hits: list[DocHit]


# ---------------- The P4 fixer model (GGUF on CPU, loaded once in the background) ----------------
import difflib                                          # Show which lines the fix changed
import re                                               # Pull the code out of the model's answer

GGUF_REPO = "alyasarmca/qwen2.5-coder-1.5b-csharp-fix-GGUF"   # Your fine-tuned model on the Hugging Face Hub
FIX_SYSTEM_PROMPT = ("You fix C# compiler errors in ASP.NET Core Program.cs files. "
                     "Return the complete corrected file in one ```csharp block. Change only what is needed to fix the errors.")   # Exactly as in P4's training
FIX_MAX_CODE_CHARS = 3_000                              # The model was trained on files up to ~2,500 characters
CODE_BLOCK = re.compile(r"```(?:csharp|cs|c#)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)   # A ```csharp block
_llm = None                                             # The model, once loaded
_llm_load_lock = threading.Lock()                       # Only one thread may load it
_llm_run_lock = threading.Lock()                        # llama.cpp handles one generation at a time


def _get_llm():
    """Download (first time only) and load the GGUF model. Anything printed goes to stderr."""
    global _llm
    with _llm_load_lock:
        if _llm is None:
            with contextlib.redirect_stdout(sys.stderr):   # Protect the stdio protocol
                from huggingface_hub import hf_hub_download, list_repo_files
                from llama_cpp import Llama
                gguf_file = next(f for f in list_repo_files(GGUF_REPO) if f.lower().endswith("q4_k_m.gguf"))
                _llm = Llama(model_path=hf_hub_download(GGUF_REPO, gguf_file), n_ctx=2048, verbose=False)
    return _llm


def _errors_for_model(errors):
    """Errors in the exact format the model was trained on (max 5)."""
    return "\n".join(f"Line {e['line']}: error {e['code']}: {e['message']}\n    > {e['source']}" for e in errors[:5])


def _changed_lines(before, after):
    """Only the removed (-) and added (+) lines between two versions."""
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    lines = [l for l in diff if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return "\n".join(lines) or "(no changes)"


class FixResult(TypedDict):                             # The result of fix_compile_errors
    compiles: bool                                      # Does the fixed code compile? (verified with a real build)
    fixed_code: str                                     # The model's corrected file ("" if nothing was attempted)
    changed_lines: str                                  # "- old / + new" lines
    original_errors: list[CompileError]                 # The errors before the fix
    remaining_errors: list[CompileError]                # Errors still left after the fix (empty if it compiles)
    note: str                                           # A one-line interpretation for the model


# ---------------- Tools ----------------
@mcp.tool()
def dotnet_info() -> str:
    """List the .NET SDK versions installed on the machine that runs this server.
    Use it to check which .NET versions are available before compiling C# code."""
    if not shutil.which("dotnet"):                      # No dotnet on this machine
        return "The .NET SDK is not installed on this server."
    r = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or r.stderr.strip()


def _compile(code):
    """Compile code and return a BuildReport (shared by compile_csharp and fix_compile_errors)."""
    if not code.strip():                                # Input guard: nothing to compile
        return _report_error("The code is empty.")
    if len(code) > MAX_CODE_CHARS:                      # Input guard: too long
        return _report_error(f"The code is too long ({len(code):,} characters; the limit is {MAX_CODE_CHARS:,}).")
    with _build_lock:                                   # Wait if another call is building right now
        result = compile_code(code)                     # Real dotnet build in the sandbox
    lines = code.splitlines()                           # To attach the offending source line to each error
    errors = [{"line": e["line"], "code": e["code"], "message": e["message"],
               "source": lines[e["line"] - 1].strip() if 0 < e["line"] <= len(lines) else ""}
              for e in result.errors[:MAX_ERRORS]]
    return {"succeeded": result.succeeded, "error_count": len(result.errors), "errors": errors,
            "seconds": result.seconds}


@mcp.tool()
def compile_csharp(code: str) -> BuildReport:
    """Compile a complete C# Program.cs for an ASP.NET Core 10 web app and return the compiler errors.
    The project is created with `dotnet new web`: implicit usings are on and no extra NuGet packages are available.
    The code is only compiled, never run. Always pass the WHOLE file, not a fragment.
    Use it to check that C# code actually builds, and to get exact errors (line, CS code, message, source line) to fix."""
    return _compile(code)


@mcp.tool()
def find_namespace(name: str) -> NamespaceResult:
    """Find which namespace (and `using` directive) a .NET 10 / ASP.NET Core 10 type or extension method lives in.
    Use it for compiler errors CS0246 / CS0103 ("type or namespace could not be found", "name does not exist") and for
    CS1061 when the missing member is an extension method. Pass just the name, e.g. "JsonOptions", "Hub", "FromForm",
    "DrainAsync", "MediaTypeNames". If the name doesn't exist, close matches are suggested (catches typos and invented APIs).
    If the namespace is already an implicit using in web projects, the cause is something else."""
    if not name.strip() or len(name) > MAX_NAME_CHARS:  # Input guard
        return {"query": name[:MAX_NAME_CHARS], "matches": [], "suggestions": [], "sdk": "",
                "note": "Please pass a single type or method name."}
    return api_index.find_namespace(name)


@mcp.tool()
def list_members(type_name: str) -> MembersResult:
    """List the public methods, properties and fields of an ASP.NET Core 10 or Microsoft.Extensions type,
    e.g. "OutputCachePolicyBuilder", "HttpResponse" or a full name like "Microsoft.AspNetCore.Http.HttpResponse".
    Use it when the compiler says a type "does not contain a definition for X" (CS1061 / CS0117):
    look up the real member names instead of guessing. Returns names only (no signatures)."""
    if not type_name.strip() or len(type_name) > MAX_NAME_CHARS:   # Input guard
        return {"type": "", "members": [], "candidates": [], "note": "Please pass a single type name."}
    return api_index.list_members(type_name)


@mcp.tool()
def search_aspnetcore_docs(query: str, k: int = DOCS_DEFAULT_K) -> DocsResult:
    """Search the official ASP.NET Core 10 documentation (Blazor, Fundamentals, MVC) and return the most relevant
    passages with their URLs. Uses hybrid search (vectors + BM25) and a cross-encoder reranker.
    Use it to check the correct, current way to implement something (middleware, rate limiting, output caching,
    options, authentication...) before writing code, or to explain a concept with a source. k = passages (1-8)."""
    query = query.strip()[:500]                         # Input guard: long queries are cut
    if not query:
        return {"query": "", "hits": []}
    k = max(1, min(int(k), DOCS_MAX_K))                 # Keep k between 1 and 8
    with contextlib.redirect_stdout(sys.stderr):        # Protect the stdio protocol during the search too
        hits = _get_rag().retrieve(query, k=k)
    return {"query": query, "hits": [{"rank": i, "section": h["where"], "url": h["url"], "text": h["text"]}
                                     for i, h in enumerate(hits, start=1)]}


def fix_compile_errors(code: str) -> FixResult:
    """Try to fix the compiler errors in a C# ASP.NET Core Program.cs with a small fine-tuned model (Qwen2.5-Coder-1.5B, QLoRA),
    then verify the fix with a real build. Pass the WHOLE broken file (max 3,000 characters); errors are found automatically.
    Good at: missing ; ) or }, misspelled variables and types, wrong literal types, missing `async`, missing `new`.
    NOT good at: API names it doesn't know (e.g. a method that doesn't exist) or which `using` a type needs:
    for those, use list_members and find_namespace instead. Always check `compiles` in the result."""
    if len(code) > FIX_MAX_CODE_CHARS:                  # Input guard: longer than anything the model was trained on
        return {"compiles": False, "fixed_code": "", "changed_lines": "", "original_errors": [], "remaining_errors": [],
                "note": f"The code is too long for the fixer model (max {FIX_MAX_CODE_CHARS:,} characters)."}
    before = _compile(code)                             # 1) Get the real errors
    if before["succeeded"]:
        return {"compiles": True, "fixed_code": code, "changed_lines": "(no changes)", "original_errors": [],
                "remaining_errors": [], "note": "The code already compiles: nothing to fix."}
    if before["errors"] and before["errors"][0]["code"] == "INPUT":   # Empty code etc.
        return {"compiles": False, "fixed_code": "", "changed_lines": "", "original_errors": before["errors"],
                "remaining_errors": before["errors"], "note": before["errors"][0]["message"]}
    user = f"Compiler errors:\n{_errors_for_model(before['errors'])}\n\nCode:\n```csharp\n{code.strip()}\n```"   # Same format as training
    with _llm_run_lock, contextlib.redirect_stdout(sys.stderr):   # 2) Ask the model (one at a time)
        out = _get_llm().create_chat_completion(
            messages=[{"role": "system", "content": FIX_SYSTEM_PROMPT}, {"role": "user", "content": user}],
            temperature=0.0, max_tokens=1024)
    answer = out["choices"][0]["message"]["content"]
    m = CODE_BLOCK.search(answer)
    fixed = (m.group(1) if m else answer).strip() + "\n"   # The code inside the ```csharp block
    after = _compile(fixed)                             # 3) Verify: does the fix really compile?
    note = ("Fixed: the corrected code compiles." if after["succeeded"] else
            "The fix does not compile. If the remaining errors are about unknown names or missing usings, "
            "try list_members / find_namespace.")
    return {"compiles": after["succeeded"], "fixed_code": fixed, "changed_lines": _changed_lines(code.strip(), fixed.strip()),
            "original_errors": before["errors"], "remaining_errors": after["errors"], "note": note}

if ENABLE_FIXER:                                        # Publish the fixer tool only when enabled
    mcp.tool()(fix_compile_errors)                      # Same as writing @mcp.tool() above the function

if __name__ == "__main__":
    mcp.run(transport="stdio")                          # stdio: the client starts this script and talks over stdin/stdout