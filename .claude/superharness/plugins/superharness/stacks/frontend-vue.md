# Frontend stack: Vue

This project's frontend is **Vue 3**. Apply these conventions when working here.

## Layout
- `src/components/` reusable components (`<script setup>` SFCs, PascalCase filenames).
- `src/views/` route-level views. `src/composables/` composition functions (`useX`).
- `src/stores/` Pinia stores. `src/lib/` framework-agnostic helpers.

## Testing (TDD — write the failing test first)
- Test runner: **Vitest**. Component tests: **@vue/test-utils** + `@testing-library/jest-dom`.
- Run all: `npm run test`. Single file: `npx vitest run src/components/Foo.spec.ts`.
- Mount the component and assert on rendered output and emitted events, not internal refs.

## Standards
- TypeScript + `<script setup>`. Composition API only (no Options API for new code).
- Keep components small; extract logic into composables. Co-locate `Foo.vue` + `Foo.spec.ts`.
- Lint/format with ESLint + Prettier; run `npm run lint` before claiming done.
