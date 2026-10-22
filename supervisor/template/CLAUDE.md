# Claude project entry point

Read @AGENTS.md first; it is the shared instruction set for every agent CLI working in this project.

You are working inside a project run by the Supervisor framework. Read `README.md` (resume protocol), then `STATE.md`
and `TASKS.md`. Your packet (the brief you were dispatched with) names your role, the task contract, the artifacts you
must produce, the tests you must run, and the paths you may write. Stay inside them: verification compares the
working tree before and after your attempt.

- The task store under `state/` is the truth; rendered `*.md` files are projections — never edit them.
- Never edit `state/` directly while the supervisor runs; use `supervisor <command>` (they queue through the loop).
- Credentials live only in git-ignored `secrets/`; never in a prompt, a log, a file you write.
- No commit, push, reset or history rewrite: the supervisor checkpoints accepted work itself.
- Finish with the fenced JSON result block your packet asks for (`agents/schemas/<kind>.json`).
- Near a usage limit: write `HANDOFF.md` in your run directory and end with an honest PARTIAL.

Project-specific rules, if any, are in `docs/plan/PROJECT_PLAN.md` and the role cards under `agents/`.
