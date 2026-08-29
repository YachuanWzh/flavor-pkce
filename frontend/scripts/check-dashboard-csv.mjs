// Standalone assertion for src/dashboard-csv.js (the project has no JS
// test framework — see the plan's frontend TDD exemption). Run with:
//   node scripts/check-dashboard-csv.mjs
import assert from "node:assert";
import { rowsToCsv, buildDashboardCsv } from "../src/dashboard-csv.js";

assert.equal(rowsToCsv([]), "");
assert.equal(rowsToCsv(null), "");

const one = rowsToCsv([{ a: 1, b: "x" }, { a: 2, b: 'q"z' }]);
assert.equal(one, 'a,b\n1,x\n2,"q""z"');

// Union of keys across rows; missing cells stay empty.
assert.equal(rowsToCsv([{ a: 1 }, { b: 2 }]), "a,b\n1,\n,2");

// Formula-injection guard (review fix): dangerous leading chars get the
// Excel text-prefix so exported cells never execute as formulas.
for (const bad of ["=SUM(A1)", "+x", "-2", "@evil", "\ttab"]) {
  const line = rowsToCsv([{ svc: bad }]).split("\n")[1];
  assert(line.startsWith("'"), `formula prefix missing: ${line}`);
}
assert(rowsToCsv([{ a: "plain" }]).endsWith("plain"));

const csv = buildDashboardCsv({
  tokens: [{ date: "2026-08-28", prompt_tokens: 5, note: "a,b" }],
  requests: [{ date: "2026-08-28", requests: 3, errors: 0 }],
  models: [{ model: "=hy.bin(cmd)", total_tokens: 1 }],
  users: null,
});
assert(csv.includes("# daily_tokens"), "section header");
assert(csv.includes("date,prompt_tokens,note"), "union header");
assert(csv.includes('"a,b"'), "field quoting");
assert(csv.includes("'=hy.bin(cmd)"), "model cell defused");
assert(!csv.includes("# users"), "empty sections omitted");
assert(csv.endsWith("\n"), "trailing newline");

console.log("dashboard-csv checks OK");
