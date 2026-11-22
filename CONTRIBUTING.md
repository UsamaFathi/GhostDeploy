# Contributing

Use Python 3.11 or later and install locally with `python -m pip install -e '.[dev]'`. Before submitting changes, run `pytest`, `ruff check .`, `ruff format --check .`, `python -m build` and `python -m twine check --strict dist/*`. The Linux lock test requires Linux. Git and Bash fixtures use local temporary working directories and a stub SSH command.

Keep configuration validation independent of the host. Use structured subprocess arguments, sanitized user-facing errors and explicit database allowlists. Exercise deployment failures through temporary directories and simulated services in CI. Route PostgreSQL and systemd integration testing to a disposable, separately provisioned environment. Read [operations](docs/operations.md) before working on any deployment transitions.
