# Fullstack: React + Python

This project is **fullstack**: a **React** frontend and a **Python** backend. Apply both stacks'
conventions (see the React and Python guidance above) plus the seam rules below.

## Layout
- `frontend/` React app; `backend/` Python app. Keep them independently testable.

## Frontend (React)
- Vitest + @testing-library/react. TypeScript strict. Small pure components, side effects in hooks.

## Backend (Python)
- pytest. Type hints + mypy. Thin API layer (FastAPI), logic in services. black + ruff.

## The seam (React <-> Python)
- **API contract is the contract.** Define request/response shapes once; mirror them as TS types
  on the frontend. Change them in lockstep and update tests on both sides in the same task.
- **CORS:** backend allows the dev frontend origin; do not disable CORS globally.
- **Dev proxy:** frontend dev server proxies `/api` to the backend to avoid origin mismatch.
- **End-to-end:** cover at least one real frontend->backend flow with an e2e/integration test.

## Discipline
- TDD on both sides. Run frontend (`npm run test`) and backend (`pytest`) suites before claiming done.
