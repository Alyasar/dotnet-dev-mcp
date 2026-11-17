# Third-party content

This project's code is MIT-licensed (see LICENSE). It includes or downloads the following third-party material, under their own licenses:

| Material | Where | License |
|---|---|---|
| Text of the ASP.NET Core documentation (chunked, in `data/processed/chunks.jsonl` and the vector index in `data/chroma/`) | [dotnet/AspNetCore.Docs](https://github.com/dotnet/AspNetCore.Docs), © Microsoft | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (documentation); code samples MIT |
| API index (`data/api_index.json`): public type, namespace and member names read from the .NET 10 reference assemblies | .NET SDK, © .NET Foundation and Contributors | MIT |
| Embedding model `BAAI/bge-small-en-v1.5` (downloaded on first use) | Hugging Face | MIT |
| Reranker `cross-encoder/ms-marco-MiniLM-L6-v2` (downloaded on first use) | Hugging Face | Apache 2.0 |
| Fixer model `alyasarmca/qwen2.5-coder-1.5b-csharp-fix-GGUF` (optional, downloaded on first use), fine-tuned from `Qwen/Qwen2.5-Coder-1.5B-Instruct` | Hugging Face | Apache 2.0 |
