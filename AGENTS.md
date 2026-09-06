# Repository guidelines

- Keep the application local-first and single-process unless a new request changes scope.
- `Profile` is the canonical definition. Generate every artifact through `generators.py`.
- `Source=AI` is the only extraction switch. Do not serialize the full Profile into extraction requests.
- Preserve the system fields and SQL rules confirmed in `docs/design.md`.
- Use lowercase snake_case column names with exact agreement across all artifacts.
- Do not add database columns absent from Field Mapping or execute generated SQL against production.
- Do not commit `data/`, API keys, real business PDFs/images, extracted values, or downloaded exports.
- Keep endpoint credentials in memory/environment; never echo settings request bodies in errors.
- Run `uv run pytest -q` for logic changes and check the browser when changing UI behavior.
- Document any MVP limitations honestly. Mock-provider tests are not verification of a real Qwen/Kimi endpoint or Datara import.
