"""Streamlit playground for the dotnet-dev-tools MCP server: discover its tools and call them over real MCP."""

import asyncio                                          # The MCP client is asynchronous
import json                                             # Pretty-print results
import os                                               # Environment for the server process
import sys                                              # sys.executable: start the server with this Python
import threading                                        # One background thread keeps the MCP connection open

import streamlit as st                                  # The web UI

st.set_page_config(page_title="dotnet-dev-tools MCP", page_icon="🧰", layout="wide")

CALL_TIMEOUT = 300                                      # Seconds per tool call (first calls wait for models to load)
MAX_CALLS_PER_SESSION = 30                              # Abuse guard per visitor session


class McpConnection:
    """Starts server.py once and keeps an MCP client session open in a background event loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()            # A private event loop for the MCP client
        self.ready = threading.Event()                  # Set when the handshake is done
        self.session, self.tools, self.error = None, [], None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()   # Run the loop in the background
        asyncio.run_coroutine_threadsafe(self._run(), self.loop)              # Connect inside that loop

    async def _run(self):
        from mcp import ClientSession                   # Imported here: only needed inside the loop
        from mcp.client.stdio import StdioServerParameters, stdio_client
        try:
            server = StdioServerParameters(command=sys.executable, args=["server.py"], env=dict(os.environ))   # Full env: PATH/DOTNET_ROOT for builds
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()          # The MCP handshake
                    self.tools = (await session.list_tools(params=None)).tools   # Tool discovery
                    self.session = session
                    self.ready.set()
                    await asyncio.Event().wait()        # Keep the connection open forever
        except Exception as e:                          # Startup failed: show it in the page
            self.error = f"{type(e).__name__}: {e}"
            self.ready.set()

    def call(self, tool, arguments):
        """Call one tool from Streamlit's (normal, synchronous) code and return (structured result, text, is_error)."""
        future = asyncio.run_coroutine_threadsafe(
            self.session.call_tool(tool, arguments, read_timeout_seconds=CALL_TIMEOUT), self.loop)
        result = future.result(timeout=CALL_TIMEOUT + 10)
        text = " ".join(c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text")
        return getattr(result, "structured_content", None), text, bool(getattr(result, "is_error", False))


@st.cache_resource(show_spinner="Starting the MCP server (first start on the cloud installs .NET: up to 2–3 minutes)…")
def connect():
    from dotnet_setup import ensure_dotnet              # Installs the .NET SDK on Linux hosts; does nothing on your PC
    ensure_dotnet()
    conn = McpConnection()
    conn.ready.wait(timeout=600)                        # Wait for the handshake
    return conn


# ---------------- Page ----------------
st.title("🧰 dotnet-dev-tools: an MCP server for .NET developers")
st.markdown(
    "An **MCP server** that gives AI assistants real .NET knowledge instead of guesses: it **compiles C#** with the .NET 10 SDK, "
    "finds the **namespace** of any .NET type, lists the **real members** of ASP.NET Core types, and **searches the official docs**.  \n"
    "This page talks to the server over the **real MCP protocol** (like Claude Desktop or GitHub Copilot would). "
    "First calls are slow while the models load.")

conn = connect()
if conn.error or not conn.session:
    st.error(f"The MCP server could not start: {conn.error or 'timeout'}")
    st.stop()

with st.expander(f"🔍 Tool discovery: {len(conn.tools)} tools (exactly what an AI assistant sees)"):
    for t in conn.tools:
        st.markdown(f"**`{t.name}`**")
        st.caption(t.description or "")
        st.json(t.input_schema, expanded=False)

calls = st.session_state.setdefault("calls", 0)
names = [t.name for t in conn.tools if t.name != "dotnet_info"]
tool = st.radio("Tool", names, horizontal=True)

EXAMPLE_CODE = ('var builder = WebApplication.CreateBuilder(args);\nbuilder.Services.AddOutputCache();\n\nvar app = builder.Build();\n'
                'app.UseOutputCache();\n\napp.MapGet("/time", () => DateTime.Now.ToString()).CacheOutput(p => p.WithDuration(10));\n\napp.Run();')
if tool in ("compile_csharp", "fix_compile_errors"):
    args = {"code": st.text_area("Program.cs", EXAMPLE_CODE, height=260)}
elif tool == "find_namespace":
    args = {"name": st.text_input("Type or extension method name", "JsonOptions")}
elif tool == "list_members":
    args = {"type_name": st.text_input("Type name", "OutputCachePolicyBuilder")}
else:                                                   # search_aspnetcore_docs
    args = {"query": st.text_input("Question", "How do I write custom middleware?"),
            "k": st.slider("Passages", 1, 8, 4)}

if st.button("Call tool", type="primary", disabled=calls >= MAX_CALLS_PER_SESSION):
    st.session_state["calls"] += 1
    with st.spinner(f"Calling {tool} over MCP…"):
        data, text, is_error = conn.call(tool, args)
    if is_error:
        st.error(text[:1000])
    elif tool == "compile_csharp":
        (st.success if data["succeeded"] else st.error)(
            f"{'✅ Builds' if data['succeeded'] else '❌ Does not build'} ({data['seconds']} s, {data['error_count']} error(s))")
        for e in data["errors"]:
            st.code(f"Line {e['line']}: error {e['code']}: {e['message']}\n    > {e['source']}", language="text", wrap_lines=True)
    elif tool == "find_namespace":
        st.info(data["note"])
        for m in data["matches"]:
            st.code(m["using"] + f"   // {m['kind']}, {m['detail']}" + ("  (already implicit)" if m["already_implicit"] else ""), language="csharp")
    elif tool == "list_members":
        st.info(data["note"])
        st.write(", ".join(f"`{m}`" for m in data["members"]) or ", ".join(data["candidates"]))
    elif tool == "search_aspnetcore_docs":
        for h in data["hits"]:
            st.markdown(f"**{h['rank']}. [{h['section']}]({h['url']})**")
            st.caption(h["text"][:600] + ("…" if len(h["text"]) > 600 else ""))
    elif tool == "fix_compile_errors":
        (st.success if data["compiles"] else st.warning)(data["note"])
        st.code(data["changed_lines"], language="diff")
    with st.expander("Raw MCP result (JSON)"):
        st.code(json.dumps(data, indent=2) if data is not None else text, language="json")
if calls >= MAX_CALLS_PER_SESSION:
    st.info("Call limit for this session reached. Refresh the page later.")

st.caption("Source: github.com/Alyasar/dotnet-dev-mcp · Evaluated with gpt-oss-20b: see the README for results.")