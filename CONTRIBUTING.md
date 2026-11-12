# Contributing

Issues and small pull requests are welcome. This is a deliberately small reference build, so changes
that add services or abstractions are unlikely to be merged.

- Run the checks before you open a pull request:
  `uv run ruff check && uv run ruff format --check && uv run ty check && uv run pytest -q` and
  `terraform fmt -check -recursive infra`.
- Use synthetic data only. No account IDs, ARNs with account IDs, credentials or state files.
- A change to a control should come with its proof or test, and an update to the risk table in
  `docs/architecture.md`.
- Report security problems privately: see [SECURITY.md](SECURITY.md).
