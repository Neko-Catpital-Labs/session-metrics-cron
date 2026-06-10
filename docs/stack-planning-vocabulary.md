# Stack Planning Vocabulary

This document defines the core vocabulary for stack planning models and graph visualizations.

## Task

A task is a larger unit of work or goal.

A task can be decomposed into one or more actions. It is useful for describing the user-facing objective, but it is usually too broad to be a precise graph node by itself.

Example:

```text
Task: Add config-driven behavior
```

## Action

An action is a concrete, observable unit of work.

An action is specific enough to appear as a plan step or become one PR in a stack. In the graph model, actions should be the primary nodes.

Examples:

```text
Add config schema
Add config reader
Implement config-driven behavior
Run terminal verification
Remove migrated cleanup paths
```

## Tag

A tag is a qualifier or category attached to an action.

A tag describes what kind of work an action is. Tags should be metadata on action nodes, not standalone graph nodes.

Examples:

```text
foundation
behavior
compatibility
verification
cleanup
config
```

## Rule

A rule is a constraint between actions.

Most rules express ordering, such as "Action A should happen before Action B." Rules may also express splitting or sequencing constraints, but they should still apply to actions rather than treating tags as standalone work.

Example:

```text
Add config schema -> Implement config-driven behavior
```

## Relationship

The intended model is:

```text
Task
  contains Actions

Action
  has Tags

Rule
  constrains Actions
```

Concrete example:

```text
Task: Add config-driven behavior

Actions:
  1. Add config schema
     tags: foundation, config

  2. Add config reader
     tags: foundation, config

  3. Implement config-driven behavior
     tags: behavior, config

Rule:
  Add config schema -> Implement config-driven behavior
```

## Important Modeling Constraint

Tags should not be rendered as independent graph nodes.

If a generated model contains an item like `tag:foundation`, treat it as a category that should be attached to one or more action nodes. It should not be interpreted as a concrete task or action by itself.
