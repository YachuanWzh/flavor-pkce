# Superharness Bootstrap

You have superharness: a project-level engineering discipline harness. It is loaded
as a Claude Code plugin from the local marketplace at `.claude/superharness` and this
document is injected at session start by its SessionStart hook.

## The Rule

**Before responding to ANY engineering task — including "simple" ones — check whether a
superharness skill applies. If there is even a 1% chance a skill applies, invoke it with
the Skill tool before doing anything else.**

## Available Skills

| Skill | Invoke when |
|-------|-------------|
| `superharness:go` | The user gives a task goal to complete end-to-end (also triggered by `/superharness:go <goal>`) |
| `superharness:brainstorm` | ONLY when the user explicitly runs `/superharness:brainstorm <topic>` — never self-invoke. Requirements/design dialogue with a live browser mind map |
| `superharness:writing-plans` | A multi-step task needs an implementation plan, before touching code |
| `superharness:using-git-worktrees` | Starting feature work that needs an isolated workspace, before implementation (go Phase 0.5) |
| `superharness:subagent-driven-development` | Executing a multi-task plan with independent tasks in the current session (go Phase 2) |
| `superharness:test-driven-development` | Implementing ANY feature or bugfix, before writing implementation code |
| `superharness:systematic-debugging` | Any bug, test failure, or unexpected behavior, before proposing fixes |
| `superharness:requesting-code-review` | A task or major feature is complete, before moving on or merging |
| `superharness:verification-before-completion` | About to claim work is complete, fixed, or passing |

## Non-Negotiable Constraints

1. **TDD always.** Write the failing test first, watch it fail (RED), write minimal code,
   watch it pass (GREEN), refactor, commit. Implementation code written before its test
   gets deleted and redone. See `superharness:test-driven-development`.
2. **Evidence before claims.** Never say "done", "fixed", or "passing" without running the
   verification command in this session and seeing the output. See
   `superharness:verification-before-completion`.
3. **Root cause over quick fix.** When something breaks, follow the 4-phase process in
   `superharness:systematic-debugging`. No "let me just try this" patches.
4. **Plan before multi-step work.** Tasks with 3+ steps get a written plan
   (`superharness:writing-plans`) saved to `.claude/superharness/plans/` in the project, with
   bite-sized 2–5 minute TDD steps.
5. **Review before declaring victory.** Significant changes get a code review pass
   (`superharness:requesting-code-review`); Critical issues block progress.

## Red Flags — STOP if you catch yourself thinking:

| Thought | Reality |
|---------|---------|
| "This is too simple for TDD" | Simple things break too. Test first. |
| "I'll write tests after" | That's not TDD. RED comes first. |
| "It should work now" | Run it. Show the output. Then say it works. |
| "Let me just try changing this" | That's guessing, not debugging. Find the root cause. |
| "The plan is in my head" | Write it down. Plans in heads evaporate. |
