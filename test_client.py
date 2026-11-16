"""A small MCP client: starts server.py over stdio, lists its tools and calls them (with a long timeout)."""

import asyncio                                          # MCP clients are asynchronous
import sys                                              # sys.executable: this venv's python, to start the server
import time                                             # Measure how long each call takes

from mcp import ClientSession, MCPError                 # The client session and its error type (McpError in v1)
from mcp.client.stdio import StdioServerParameters, stdio_client   # Start a server as a child process and talk over stdio

TIMEOUT = 300                                           # Seconds to wait for one tool call (fixes run on CPU)

FIX_TESTS = {                                           # name -> broken code for fix_compile_errors
    "1 missing semicolon": 'var builder = WebApplication.CreateBuilder(args);\nvar app = builder.Build();\n\n'
                           'app.MapGet("/", () => "Hello World!")\n\napp.Run();\n',
    "2 misspelled variable": 'var builder = WebApplication.CreateBuilder(args);\nbuilder.Services.AddOutputCache();\n\n'
                             'var app = buider.Build();\napp.UseOutputCache();\n\n'
                             'app.MapGet("/time", () => DateTime.Now.ToString()).CacheOutput();\n\napp.Run();\n',
    "3 invented API": 'var builder = WebApplication.CreateBuilder(args);\nbuilder.Services.AddOutputCache();\n\n'
                      'var app = builder.Build();\napp.UseOutputCache();\n\n'
                      'app.MapGet("/time", () => DateTime.Now.ToString()).CacheOutput(p => p.WithDuration(10));\n\napp.Run();\n',
}


async def call(session, tool, arguments):
    """Call one tool and return its structured result, or None if the call failed."""
    start = time.time()
    try:
        result = await session.call_tool(tool, arguments, read_timeout_seconds=TIMEOUT)
    except MCPError as e:                               # Timeouts and protocol errors
        print(f"   ERROR after {time.time() - start:.0f} s: {e}")
        return None
    seconds = time.time() - start
    text = " ".join(c.text for c in result.content if c.type == "text")   # The text part of the result
    if result.is_error:                                 # The server reported an error (e.g. unknown tool)
        print(f"   TOOL ERROR ({seconds:.1f} s): {text[:300]}")
        return None
    print(f"   ({seconds:.1f} s)")
    return result.structured_content                    # The typed result (our tools always return one)


async def main():
    server = StdioServerParameters(command=sys.executable, args=["server.py"])   # Same command the Inspector used
    async with stdio_client(server) as (read, write):   # Start server.py and connect to its stdin/stdout
        async with ClientSession(read, write) as session:
            await session.initialize()                  # The MCP handshake
            tools = await session.list_tools(params=None)
            print("Tools:", [t.name for t in tools.tools])

            print("\nSearch: fixed window rate limiter return 429")
            docs = await call(session, "search_aspnetcore_docs", {"query": "fixed window rate limiter return 429"})
            for hit in (docs or {}).get("hits", []):
                print(f"   {hit['rank']}. {hit['section']}\n      {hit['url']}")

            for name, code in FIX_TESTS.items():        # The 3 fix tests
                print(f"\nFix test {name}")
                r = await call(session, "fix_compile_errors", {"code": code})
                if r:
                    print(f"   compiles: {r['compiles']}\n   changed:\n      " + r["changed_lines"].replace("\n", "\n      "))
                    print(f"   note: {r['note']}")


asyncio.run(main())                                     # Run the async main function