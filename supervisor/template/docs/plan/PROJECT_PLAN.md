# {{ name }} — project plan

_Written by the setup interview and approved by the owner before any task runs. Keep it current: the Chief Architect
rules from it, and the Senior PM plans from it._

## 1. Goal and definition of done

## 2. Constraints that bind
(platform, runtime, budget, time, hardware, policy, what must never leave this machine)

## 3. Environment
(repo, language/toolchain, test command, how to run the thing, external services and their credentials in `secrets/`)

## 4. Roles
(which roles are active, which lane each runs on, what each may decide — mirrors `config/routing.yaml` and `agents/`)

## 5. Phases and gates
(one row per phase: goal, exit gate, evidence the gate needs, whether the owner must acknowledge — mirrors `state/gates.yaml`;
then the first phase's task list — mirrors `state/tasks_seed.yaml`)

## 6. Tracker
(what a task record must carry for this project beyond the standard contract; owner-task conventions)

## 7. Owner decisions already taken
(numbered; each becomes a `state/decisions/D-nnn-*.md` record)

## 8. Open questions
