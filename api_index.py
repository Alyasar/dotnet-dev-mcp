"""Look up .NET 10 types, extension methods and members in the index built by the indexer (data/api_index.json)."""

import difflib                                          # Close matches for misspelled or invented names
import json                                             # Read the index file
from functools import lru_cache                         # Load the index only once
from pathlib import Path                                # File paths that work on any OS

INDEX_FILE = Path(__file__).resolve().parent / "data" / "api_index.json"   # Written by the C# indexer (indexer/)
MAX_RESULTS = 10                                        # At most this many matches per lookup
MAX_MEMBERS = 200                                       # At most this many member names per type

IMPLICIT_USINGS = {                                     # Namespaces every `dotnet new web` project already imports
    "System", "System.Collections.Generic", "System.IO", "System.Linq", "System.Net.Http", "System.Net.Http.Json",
    "System.Threading", "System.Threading.Tasks", "Microsoft.AspNetCore.Builder", "Microsoft.AspNetCore.Hosting",
    "Microsoft.AspNetCore.Http", "Microsoft.AspNetCore.Routing", "Microsoft.Extensions.Configuration",
    "Microsoft.Extensions.DependencyInjection", "Microsoft.Extensions.Hosting", "Microsoft.Extensions.Logging"}


@lru_cache(maxsize=1)
def load_index():
    """Read the index once and build lookup tables: type name -> entries, method name -> entries, members by type."""
    data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    by_type, by_ext, by_short = {}, {}, {}
    for t in data["types"]:                             # Every public type
        by_type.setdefault(t["name"], []).append(t)
    for e in data["extension_methods"]:                 # Every public extension method
        by_ext.setdefault(e["name"], []).append(e)
    for full in data["members"]:                        # "Namespace.Type" -> its member names
        by_short.setdefault(full.rsplit(".", 1)[-1], []).append(full)
    return {"sdk": data["sdk"], "by_type": by_type, "by_ext": by_ext, "members": data["members"], "by_short": by_short}


def _clean(name):
    """'Results<Ok, NotFound>' -> 'Results'; '[FromForm]' -> 'FromForm'; 'x.DrainAsync(' -> 'DrainAsync'."""
    name = name.strip().strip("[]()").split("<")[0].split("(")[0]
    return name.rsplit(".", 1)[-1].strip()              # Keep only the last part of a dotted name


def find_namespace(name):
    """Which namespace (using directive) a type or extension method lives in, plus close matches if unknown."""
    ix, query = load_index(), _clean(name)
    matches, seen = [], set()
    candidates = [("type", t) for t in ix["by_type"].get(query, []) + ix["by_type"].get(query + "Attribute", [])]
    candidates += [("extension method", e) for e in ix["by_ext"].get(query, [])]   # [FromForm] is FromFormAttribute
    for kind, item in candidates:
        key = (kind, item["name"], item["namespace"])
        if key in seen:                                 # Same name in the same namespace (e.g. many overloads)
            continue
        seen.add(key)
        matches.append({"kind": kind, "name": item["name"], "namespace": item["namespace"],
                        "using": f"using {item['namespace']};",
                        "already_implicit": item["namespace"] in IMPLICIT_USINGS,
                        "detail": f"in class {item['class']}" if kind == "extension method" else f"assembly {item['assembly']}"})
    pool = list(ix["by_type"]) + list(ix["by_ext"])     # All known names
    pool += [n[:-len("Attribute")] for n in ix["by_type"] if n.endswith("Attribute")]   # Also "FromForm" for FromFormAttribute
    suggestions = [] if matches else difflib.get_close_matches(query, pool, n=5, cutoff=0.75)
    if not matches:
        note = "No type or extension method with this exact name exists in .NET 10 / ASP.NET Core 10."
        note += f" Did you mean: {', '.join(suggestions)}?" if suggestions else " It may be a typo, an invented API, or need a NuGet package."
    elif all(m["already_implicit"] for m in matches):
        note = "This namespace is already imported implicitly in web projects, so a missing using is NOT the cause: check for a typo or a wrong API."
    else:
        note = "Add the using directive for the namespace that fits your code."
    return {"query": query, "matches": matches[:MAX_RESULTS], "suggestions": suggestions, "note": note, "sdk": ix["sdk"]}


def list_members(type_name):
    """Public member names of an ASP.NET Core / Microsoft.Extensions type (short or full name)."""
    ix, query = load_index(), type_name.strip().split("<")[0]
    if query in ix["members"]:                          # Full name, e.g. Microsoft.AspNetCore.OutputCaching.OutputCachePolicyBuilder
        full = [query]
    else:
        full = ix["by_short"].get(query.rsplit(".", 1)[-1], [])   # Short name, e.g. OutputCachePolicyBuilder
    if len(full) == 1:
        members = ix["members"][full[0]]
        return {"type": full[0], "members": members[:MAX_MEMBERS], "candidates": [],
                "note": f"{len(members)} public members." + (" (list shortened)" if len(members) > MAX_MEMBERS else "")}
    if len(full) > 1:                                   # The same short name in several namespaces
        return {"type": "", "members": [], "candidates": full[:MAX_RESULTS],
                "note": "Several types have this name: call again with one of the full names."}
    close = difflib.get_close_matches(query.rsplit(".", 1)[-1], list(ix["by_short"]), n=5, cutoff=0.75)
    return {"type": "", "members": [], "candidates": close,
            "note": "Type not found among ASP.NET Core / Microsoft.Extensions types." + (f" Did you mean: {', '.join(close)}?" if close else "")}