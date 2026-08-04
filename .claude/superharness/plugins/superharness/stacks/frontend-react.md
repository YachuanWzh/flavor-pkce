# Frontend stack: React

This project's frontend is **React**. Apply these conventions when working here.

## Layout
- `src/components/` reusable components (one component per file, PascalCase).
- `src/pages/` or `src/routes/` route-level components.
- `src/hooks/` custom hooks (`useX` naming). `src/lib/` framework-agnostic helpers.

## Testing (TDD — write the failing test first)
- Test runner: **Vitest**. Component tests: **@testing-library/react** + `@testing-library/jest-dom`.
- Run all: `npm run test`. Single file: `npx vitest run src/components/Foo.test.tsx`.
- Test behavior through the rendered DOM (roles, text, user events), not implementation details.
- User interaction via `@testing-library/user-event`, not raw `fireEvent` where avoidable.

## Standards
- TypeScript, strict mode. Function components + hooks only (no class components).
- Keep components small and pure; lift side effects into hooks. Co-locate `Foo.tsx` + `Foo.test.tsx`.
- Lint/format with ESLint + Prettier; run `npm run lint` before claiming done.
