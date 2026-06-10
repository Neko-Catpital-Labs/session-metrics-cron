# Stack Planning Vocabulary

This document defines the core vocabulary for stack planning models and graph visualizations.

## The workflow model

A normal engineering workflow is:

```text
plan -> implementation steps -> done
```

- The **plan** is the task record itself (a title plus the ordered steps). There is
  no separate "plan" node in the graph.
- Every **implementation step** is a *change of some type* (see changeType below).
- Every step has a **verification gate**: its proof (tests/CI) should pass before
  the next step proceeds. Gates are a property of steps, not a phase at the end.
- What we learn from mined PR stacks is **what the steps are and what order they
  go in** - change-type-level ordering priors plus specific step-pair rules.

## Task

A task is a larger unit of work or goal — the plan.

A task decomposes into one or more ordered implementation steps. In artifacts it
is the `corpus/tasks.jsonl` record: `{title, actions[]}` where each action is a
step with its changeType and gate.

Example:

```text
Task: Add config-driven behavior
```

## Action (implementation step)

An action is a concrete, observable unit of work — one implementation step,
typically one PR in a stack. Actions are the primary graph nodes.

Examples:

```text
Add config schema
Add config reader
Implement config-driven behavior
Expose the config flag in the CLI
Remove migrated cleanup paths
```

## changeType (the single tag axis)

Every step is a change of exactly one type. **changeType is the single canonical
axis** — it answers both "what kind of work is this?" and feeds the ordering
priors. The approved list:

```text
foundation      new models/classes/config that later steps reference
behavior        the main functional change
surface         UI/API/CLI exposure of the behavior
dependency      dependency or vendored-code changes
refactor        internal restructuring without behavior change
compatibility   preserving or introducing a compatibility contract
docs            documentation changes
cleanup         changes that eliminate legacy code
verification    test-only steps whose whole job is strengthening the gate
                (kept for now; will collapse into gates later)
```

Retired vocabulary: the old two-axis scheme (`taskKind` + an ordering `phase` of
foundation/change/surface/verification/docs/cleanup) is gone. "change" carried no
information (every step is a change), and "implementation" as a bucket name was
the same concept under a third name. Sub-descriptors `behaviorType` and
`architectureLayer` survive as additional tags on steps.

Note: the Invoker analyzer has a separate **execution phase** enum for plan items
(planning/debugging/architecture/implementation/verification). That is when work
executes inside a plan item, not what kind of change a step is — the two
vocabularies are intentionally distinct.

## Verification gate

A gate is attached to each step: `{hasTests, scope, verifiedBy}` — whether the
step carries its own tests, the verification scope, and which later
verification-type steps prove it. Replay scoring measures `gateCoverage` /
`verificationGateCoverage` (the fraction of implementation steps with a gate).

## Rule

A rule is an ordering constraint between actions (steps).

```text
Add config schema -> Implement config-driven behavior
```

Rules constrain actions, never tags. changeType-level ordering priors exist only
as marked backoff evidence (`metadata.backoffLevel >= 1`) behind step-level rules.

## Relationship

```text
Task (the plan)
  contains ordered Actions (implementation steps)

Action
  is a change of one changeType
  has tags (behaviorType, architectureLayer, qualifiers)
  has a verification Gate

Rule
  constrains Actions (step order)
```

Concrete example:

```text
Task: Add config-driven behavior

Steps:
  1. Add config schema            change: foundation   gate: tests pass
  2. Add config reader            change: foundation   gate: tests pass
  3. Implement config behavior    change: behavior     gate: verified by #4
  4. Add integration tests        change: verification

Rule:
  Add config schema -> Implement config-driven behavior
```

## Important Modeling Constraint

Tags (changeType, behaviorType, architectureLayer, qualifiers) are metadata on
action nodes, never standalone graph nodes. If a generated model contains an item
like `change-type:foundation` as a node, treat it as a category that should be
attached to one or more action nodes.
