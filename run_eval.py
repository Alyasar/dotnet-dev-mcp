"""Evaluation: an LLM (Groq) fixes broken C# programs with no tools, the compiler only, or all MCP tools.
The agent connects to server.py as a real MCP client, discovers the tools and converts them to function calling.
Usage:  python run_eval.py --condition all_tools [--limit 2]
Results are saved after every task (resumable); the run stops cleanly at Groq's rate limit."""

import argparse                                         # Command-line options
import asyncio                                          # MCP clients are asynchronous
import difflib                                          # Compare the answer with the original code
import json                                             # Read tasks, write results, tool arguments
import re                                               # Pull the code out of the answer
import sys                                              # sys.executable: start the server with this venv's python
import time                                             # Timing
from pathlib import Path                                # File paths that work on any OS

import groq                                             # Groq API (reads GROQ_API_KEY from the environment)
from mcp import ClientSession, MCPError                 # MCP client
from mcp.client.stdio import StdioServerParameters, stdio_client

from csharp import compile_code                         # Independent scoring: our own dotnet build of the final answer

ROOT = Path(__file__).resolve().parent
TASKS_FILE = ROOT / "evals" / "tasks.jsonl"
MODEL = "openai/gpt-oss-20b"                            # Smaller model: tools should matter more; its own Groq quota
MAX_TURNS = 8                                           # LLM calls per task
MAX_TOOL_CALLS = 8                                      # Tool calls per task
MAX_RETRIES = 2                                         # Retries after a malformed tool call from the model
MAX_OUTPUT_TOKENS = 8_192                               # Upper limit for one answer (reasoning + a full Program.cs)
TPM_LIMIT = 8_000                                       # Groq free tier, gpt-oss-20b: prompt + max output must fit in 8,000 tokens
MIN_OUTPUT_TOKENS = 1_000                               # If less room than this is left, the conversation is too long
TOOL_TIMEOUT = 300                                      # Seconds per tool call (the fixer model runs on CPU)
MAX_TOOL_RESULT_CHARS = 4_000                           # Long tool results are cut before going back to the model
CONDITIONS = {                                          # Which MCP tools the model may use in each condition
    "no_tools": [],
    "compiler_only": ["compile_csharp"],
    "all_tools": ["compile_csharp", "find_namespace", "list_members", "search_aspnetcore_docs", "fix_compile_errors"],
}
SYSTEM = ("You fix C# compiler errors in an ASP.NET Core 10 Program.cs (a `dotnet new web` project: implicit usings on, "
          "no extra NuGet packages). Change only what is needed to fix the errors and never delete working code. "
          "Finish with the complete corrected file in one ```csharp block.")
TOOLS_HINT = (" You have tools: use them to check facts instead of guessing, and compile your final version "
              "before you answer.")
CODE_BLOCK = re.compile(r"```[\w#+.-]*[ \t]*\n(.*?)```", re.DOTALL)      # Any fenced block, whatever the language tag
OPEN_BLOCK = re.compile(r"```[\w#+.-]*[ \t]*\n(.*)", re.DOTALL)           # A fence that never closes (answer cut off)


class RateLimitStop(Exception):                         # Raised to stop the whole run cleanly
    pass


class TooLarge(Exception):                              # The conversation no longer fits in one request
    pass


# ---------------- Scoring (same rules as Project 4) ----------------
def _lines(code):
    return [re.sub(r"\s+", " ", line).strip() for line in code.splitlines() if line.strip()]


def line_changes(original, pred):
    """(changed, deleted) lines of pred compared with the original. A removed line that still exists
    elsewhere in pred (e.g. a reordered using) counts as moved, not deleted; its new position counts as a change."""
    a, b = _lines(original), _lines(pred)
    remaining = set(b)                                  # Every line of the answer
    changed = deleted = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "replace":                             # Lines rewritten
            changed += max(i2 - i1, j2 - j1)
        elif op == "insert":                            # Lines added (including a moved line's new position)
            changed += j2 - j1
        elif op == "delete":                            # Lines removed: only truly gone ones count as deleted
            deleted += sum(1 for line in a[i1:i2] if line not in remaining)
    return changed, deleted


def extract_code(text):
    """The code from the answer: a fenced block, an unclosed fence (cut off), or the whole text if it is clearly code."""
    for pattern in (CODE_BLOCK, OPEN_BLOCK):
        m = pattern.search(text)
        if m:
            return m.group(1).strip() + "\n"
    return text.strip() + "\n" if "WebApplication" in text else ""   # Code without any fence


def errors_text(code, errors):
    """Compiler errors in the format the tools use (max 5)."""
    lines = code.splitlines()
    return "\n".join(f"Line {e['line']}: error {e['code']}: {e['message']}\n    > "
                     + (lines[e['line'] - 1].strip() if 0 < e["line"] <= len(lines) else "") for e in errors[:5])


# ---------------- The agent ----------------
def estimate_tokens(messages, tools):
    """Rough prompt size in tokens (code is ~3 characters per token; slightly overestimated on purpose)."""
    return int((len(json.dumps(messages)) + len(json.dumps(tools))) / 3.0) + 200


def llm_call(client, messages, tools):
    """One Groq chat call, retrying malformed tool calls with feedback; stops the run at the rate limit.
    The output limit is whatever fits in the per-minute token limit (the same rule for every condition)."""
    for attempt in range(MAX_RETRIES + 1):
        budget = min(MAX_OUTPUT_TOKENS, TPM_LIMIT - estimate_tokens(messages, tools) - 100)   # Room left for the answer
        if budget < MIN_OUTPUT_TOKENS:
            raise TooLarge(f"prompt too long ({estimate_tokens(messages, tools):,} tokens)")
        try:
            kwargs = {"model": MODEL, "messages": messages, "temperature": 0, "max_completion_tokens": budget}
            if tools:                                   # No tools parameter at all in the no_tools condition
                kwargs["tools"] = tools
            return client.chat.completions.create(**kwargs)
        except groq.RateLimitError as e:                # Daily (or per-minute after retries) quota reached
            raise RateLimitStop(str(e)[:300])
        except groq.BadRequestError as e:               # The model produced an unusable tool call or output
            text = str(e)
            if not any(c in text for c in ("tool_use_failed", "output_parse_failed")) or attempt == MAX_RETRIES:
                raise
            messages = messages + [{"role": "user", "content": "Your last reply could not be used (invalid tool call or "
                                    "malformed output). Use only the listed tools with valid JSON arguments, or answer directly."}]
        except groq.APIStatusError as e:                # 413: the request is larger than the per-minute limit
            if e.status_code == 413:
                raise TooLarge(str(e)[:200])
            raise


async def solve(client, session, task, condition, mcp_tools):
    """Run the agent on one task and score the final answer."""
    allowed = CONDITIONS[condition]
    tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                "parameters": t.input_schema}}
             for t in mcp_tools if t.name in allowed]   # MCP tool schemas -> Groq function-calling format
    before = compile_code(task["broken"], sandbox="score")   # The errors the model starts from (own sandbox, not the server's)
    user = f"Compiler errors:\n{errors_text(task['broken'], before.errors)}\n\nCode:\n```csharp\n{task['broken']}\n```"
    messages = [{"role": "system", "content": SYSTEM + (TOOLS_HINT if tools else "")}, {"role": "user", "content": user}]
    tokens, calls, final, finish, start = 0, [], "", "", time.time()

    for turn in range(MAX_TURNS):
        try:
            resp = llm_call(client, messages, tools)
        except TooLarge:                                # The conversation grew too long: count as a failure
            finish = "too_large"
            break
        tokens += resp.usage.total_tokens if resp.usage else 0
        msg, finish = resp.choices[0].message, resp.choices[0].finish_reason
        if not msg.tool_calls:                          # A normal answer: done
            final = msg.content or ""
            break
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": tc.id, "type": "function",
                                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                        for tc in msg.tool_calls]})
        for tc in msg.tool_calls:                       # Run every tool the model asked for, through MCP
            name = tc.function.name
            if name not in allowed:
                content = f"Unknown tool '{name}'. Available: {', '.join(allowed)}."
            elif len(calls) >= MAX_TOOL_CALLS:
                content = "Tool call limit reached. Give your final answer now."
            else:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    result = await session.call_tool(name, args, read_timeout_seconds=TOOL_TIMEOUT)
                    data = getattr(result, "structured_content", None)
                    content = json.dumps(data) if data is not None else " ".join(
                        c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text")
                except (json.JSONDecodeError, MCPError) as e:
                    content = f"Tool call failed: {e}"
                calls.append(name)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content[:MAX_TOOL_RESULT_CHARS]})
    else:                                               # Out of turns: ask for the answer without tools
        messages.append({"role": "user", "content": "Stop using tools. Give the complete corrected file now."})
        try:
            resp = llm_call(client, messages, [])
            tokens += resp.usage.total_tokens if resp.usage else 0
            final, finish = resp.choices[0].message.content or "", resp.choices[0].finish_reason
        except TooLarge:
            finish = "too_large"

    code = extract_code(final)                          # The final file ("" if the answer has no code)
    built = compile_code(code, sandbox="score") if code.strip() else None   # Our own build, independent of the agent's claims
    compiles = bool(built and built.succeeded)
    changed, deleted = line_changes(task["fixed"], code) if task["fixed"] else (0, 0)
    faithful = compiles and deleted == 0 and changed <= 2 if task["fixed"] else compiles   # Real tasks: no reference
    return {"id": task["id"], "category": task["category"], "condition": condition, "model": MODEL,
            "compiles": compiles, "faithful": faithful, "changed": changed, "deleted": deleted,
            "tool_calls": calls, "n_tool_calls": len(calls), "tokens": tokens,
            "seconds": round(time.time() - start, 1), "finish_reason": finish, "final_code": code,
            "final_text": final[:4000],                 # The raw answer, for inspecting failures
            "remaining_errors": errors_text(code, built.errors) if built and not built.succeeded else ""}


async def main(condition, limit):
    tasks = [json.loads(l) for l in TASKS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    out = ROOT / "evals" / f"results_{condition}.jsonl"
    done = {json.loads(l)["id"] for l in out.read_text(encoding="utf-8").splitlines() if l.strip()} if out.exists() else set()
    todo = [t for t in tasks if t["id"] not in done][:limit or None]
    print(f"{condition}: {len(done)} done, {len(todo)} to go (model {MODEL})")
    client = groq.Groq(max_retries=5)                   # Retries short waits (per-minute limits) automatically

    server = StdioServerParameters(command=sys.executable, args=["server.py"])   # Our MCP server, started like any client would
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools(params=None)).tools   # Tool discovery: names, descriptions, schemas
            for task in todo:
                try:
                    row = await solve(client, session, task, condition, mcp_tools)
                except RateLimitStop as e:
                    print("Rate limit reached: stopping. Run the same command later to continue.\n", e)
                    return
                with out.open("a", encoding="utf-8") as f:   # Save straight away (resumable)
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"  {task['id']:<26} {task['category']:<15} compiles={'Y' if row['compiles'] else 'n'} "
                      f"faithful={'Y' if row['faithful'] else 'n'} tools={row['tool_calls']} "
                      f"{row['tokens']:,} tok {row['seconds']} s [{row['finish_reason']}]")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--condition", choices=list(CONDITIONS), required=True)
    p.add_argument("--limit", type=int, default=0, help="only the first N remaining tasks (0 = all)")
    a = p.parse_args()
    asyncio.run(main(a.condition, a.limit))
