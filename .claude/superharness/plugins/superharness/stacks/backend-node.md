# Backend stack: Node

This project's backend is **Node** (TypeScript). Apply these conventions when working here.

## Layout
- `src/` application code; routes thin (Express/Fastify), logic in `src/services/`.
- `src/**/*.test.ts` co-located or a `tests/` tree mirroring `src/`.

## Testing (TDD — write the failing test first)
- Test runner: **Jest** (ts-jest) or **Vitest** — follow whatever the repo already uses.
- Run all: `npm run test`. Single: `npx jest src/services/foo.test.ts` (or the vitest equivalent).
- HTTP layer: **supertest** against the real app instance. Assert on responses, not internals.

## Standards
- TypeScript, strict mode. `async/await` over raw promises; handle errors explicitly.
- Lint/format with ESLint + Prettier; run `npm run lint` and the full test suite before done.
