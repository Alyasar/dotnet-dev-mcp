// Builds a JSON index of the public API of the .NET 10 shared frameworks, read from the installed reference assemblies:
//   - every public type            -> its namespace (for "which using do I need?")
//   - every public extension method -> its namespace and class
//   - the public member names of every ASP.NET Core / Microsoft.Extensions type (for "does this method exist?")
// Usage: dotnet run --project indexer -- <output.json>
using System.Reflection;
using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;
using System.Text.Json;

var output = args.Length > 0 ? args[0] : "api_index.json";              // Where to write the index

var runtimeDir = Path.GetDirectoryName(typeof(object).Assembly.Location)!;   // ...\dotnet\shared\Microsoft.NETCore.App\10.0.x
var dotnetRoot = Path.GetFullPath(Path.Combine(runtimeDir, "..", "..", ".."));   // ...\dotnet (works on Windows, Linux, macOS)

var types = new List<object>();                                         // { name, namespace, assembly }
var extensions = new List<object>();                                    // { name, namespace, class, assembly }
var members = new SortedDictionary<string, SortedSet<string>>(StringComparer.Ordinal);   // "Namespace.Type" -> member names
var aspnetVersion = "";

foreach (var pack in new[] { "Microsoft.NETCore.App.Ref", "Microsoft.AspNetCore.App.Ref" })
{
    var (refDir, version) = FindRefDir(dotnetRoot, pack);               // e.g. ...\packs\Microsoft.AspNetCore.App.Ref\10.0.0\ref\net10.0
    var indexMembers = pack == "Microsoft.AspNetCore.App.Ref";          // Member names only for ASP.NET Core + Microsoft.Extensions
    if (indexMembers) aspnetVersion = version;
    Console.WriteLine($"{pack} {version}: {refDir}");

    foreach (var dll in Directory.GetFiles(refDir, "*.dll"))
    {
        using var stream = File.OpenRead(dll);
        using var pe = new PEReader(stream);                            // Reads metadata without loading the assembly
        if (!pe.HasMetadata) continue;
        var md = pe.GetMetadataReader();
        var assembly = Path.GetFileNameWithoutExtension(dll);

        foreach (var typeHandle in md.TypeDefinitions)
        {
            var type = md.GetTypeDefinition(typeHandle);
            if ((type.Attributes & TypeAttributes.VisibilityMask) != TypeAttributes.Public) continue;   // Public top-level types only
            var ns = md.GetString(type.Namespace);
            if (ns.Length == 0) continue;                               // Skip types without a namespace
            var name = StripArity(md.GetString(type.Name));             // List`1 -> List
            types.Add(new { name, @namespace = ns, assembly });

            var isStatic = (type.Attributes & TypeAttributes.Abstract) != 0 && (type.Attributes & TypeAttributes.Sealed) != 0;
            var names = new SortedSet<string>(StringComparer.Ordinal);

            foreach (var methodHandle in type.GetMethods())
            {
                var method = md.GetMethodDefinition(methodHandle);
                if (!IsPublic(method.Attributes)) continue;
                var methodName = md.GetString(method.Name);
                if (isStatic && HasAttribute(md, method.GetCustomAttributes(), "ExtensionAttribute"))
                    extensions.Add(new { name = methodName, @namespace = ns, @class = name, assembly });
                if ((method.Attributes & MethodAttributes.SpecialName) == 0)   // Skip constructors, get_/set_, operators
                    names.Add(methodName);
            }
            foreach (var propertyHandle in type.GetProperties())
            {
                var property = md.GetPropertyDefinition(propertyHandle);
                var accessors = property.GetAccessors();
                var isPublicProperty = (!accessors.Getter.IsNil && IsPublic(md.GetMethodDefinition(accessors.Getter).Attributes))
                                    || (!accessors.Setter.IsNil && IsPublic(md.GetMethodDefinition(accessors.Setter).Attributes));
                if (isPublicProperty) names.Add(md.GetString(property.Name));
            }
            foreach (var fieldHandle in type.GetFields())
            {
                var field = md.GetFieldDefinition(fieldHandle);
                if ((field.Attributes & FieldAttributes.FieldAccessMask) == FieldAttributes.Public
                    && (field.Attributes & FieldAttributes.SpecialName) == 0)   // Enum values and public constants (skip value__)
                    names.Add(md.GetString(field.Name));
            }
            if (indexMembers && names.Count > 0)
                members[$"{ns}.{name}"] = names;
        }
    }
}

var index = new { sdk = aspnetVersion, types, extension_methods = extensions, members };
File.WriteAllText(output, JsonSerializer.Serialize(index));
Console.WriteLine($"Types: {types.Count:N0} | extension methods: {extensions.Count:N0} | types with members: {members.Count:N0}");
Console.WriteLine($"Saved: {Path.GetFullPath(output)}");


static (string RefDir, string Version) FindRefDir(string dotnetRoot, string pack)
{
    var packDir = Path.Combine(dotnetRoot, "packs", pack);              // All installed versions of this reference pack
    string? bestDir = null;
    Version? best = null;
    foreach (var dir in Directory.GetDirectories(packDir))
    {
        var folder = Path.GetFileName(dir);                             // e.g. "10.0.0" or "10.0.0-rc.2.25502.107"
        if (!Version.TryParse(folder.Split('-')[0], out var v) || v.Major != 10) continue;   // .NET 10 only
        if (best is null || v > best) { best = v; bestDir = dir; }
    }
    if (bestDir is null) throw new InvalidOperationException($"No .NET 10 reference pack found in {packDir}");
    return (Path.Combine(bestDir, "ref", "net10.0"), Path.GetFileName(bestDir));
}

static bool IsPublic(MethodAttributes attributes) =>
    (attributes & MethodAttributes.MemberAccessMask) == MethodAttributes.Public;

static string StripArity(string name)
{
    var tick = name.IndexOf('`');                                       // Generic types are stored as Name`N
    return tick < 0 ? name : name[..tick];
}

static bool HasAttribute(MetadataReader md, CustomAttributeHandleCollection attributes, string attributeName)
{
    foreach (var handle in attributes)
    {
        var ctor = md.GetCustomAttribute(handle).Constructor;          // The attribute's constructor tells us its type
        StringHandle typeName = default;
        if (ctor.Kind == HandleKind.MemberReference)                    // Attribute type defined in another assembly
        {
            var parent = md.GetMemberReference((MemberReferenceHandle)ctor).Parent;
            if (parent.Kind == HandleKind.TypeReference)
                typeName = md.GetTypeReference((TypeReferenceHandle)parent).Name;
        }
        else if (ctor.Kind == HandleKind.MethodDefinition)              // Attribute type defined in this assembly
        {
            var declaring = md.GetMethodDefinition((MethodDefinitionHandle)ctor).GetDeclaringType();
            typeName = md.GetTypeDefinition(declaring).Name;
        }
        if (!typeName.IsNil && md.GetString(typeName) == attributeName) return true;
    }
    return false;
}