# Backend stack: Python

This project's backend is **Python**. Apply these conventions when working here.

## Layout
- `src/<package>/` application code. `tests/` mirrors the package tree.
- API layer (FastAPI routers / Django views) thin; business logic in service modules.

## Testing (TDD — write the failing test first)
- Test runner: **pytest**. Run all: `pytest`. Single test: `pytest tests/test_foo.py::test_bar -v`.
- Use fixtures for setup; parametrize for input variations. Assert on behavior/return values.
- HTTP layer: FastAPI `TestClient` / Django test client against real routes.

## Standards
- Type hints everywhere; check with `mypy`. Format with `black`, lint with `ruff`.
- Manage deps with the project's tool (`pyproject.toml` + uv/poetry, or `requirements.txt`).
- Run `ruff check` and the full `pytest` suite before claiming done.
