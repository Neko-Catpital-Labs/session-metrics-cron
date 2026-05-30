# Usage Command Attribution v4.5 Schema Migration

- Migrated schema: `usage_command_attribution_v4_5`
- Classifier revision: `classifier_v4_5`
- Basis: v4.5 keeps the v4.4 classifier behavior and renames the motivation fields for clearer dashboard semantics.

| v4.4 field | v4.5 field |
|---|---|
| `primary_why` | `request_origin` |
| `prompt_task_kind` | `work_motivation` |
| `primary_why_confidence` | `request_origin_confidence` |
| `prompt_task_kind_confidence` | `work_motivation_confidence` |
| `deterministic_primary_why` | `deterministic_request_origin` |
| `deterministic_prompt_task_kind` | `deterministic_work_motivation` |
| `codex_primary_why` | `codex_request_origin` |
| `codex_prompt_task_kind` | `codex_work_motivation` |
| `proposed_primary_why` | `proposed_request_origin` |
| `proposed_prompt_task_kind` | `proposed_work_motivation` |

Compatibility note: v4.5 is a breaking event schema for these motivation fields. It does not emit the old `primary_why` or `prompt_task_kind` aliases.

Dashboard note: existing Mixpanel dashboard report definitions were updated in place to use `usage_command_attribution_v4_5`, `classifier_v4_5`, and the renamed motivation properties.
