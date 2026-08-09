# skillopt

> Integrates Microsoft's SkillOpt text-space optimizer as a self-tuning layer on the agent's tool-selection loop. Periodically reviews the agent's tool-call patterns, suggests new skills to merge, and prunes unused ones.

**Version:** 1.0.0 · **Plugin ID:** `skillopt`

## Purpose

Integrates Microsoft's SkillOpt text-space optimizer as a self-tuning layer on the agent's tool-selection loop. Periodically reviews the agent's tool-call patterns, suggests new skills to merge, and prunes unused ones.

## Ownership / Layout

- `extensions/` — WebUI status + suggestion list
- `helpers/` — SkillOpt wrapper, suggestion evaluator

## Local Contracts

- SkillOpt suggestions are PROPOSED, never auto-applied. The user reviews each suggestion in the WebUI before it is merged into the agent's skill library.
- The optimizer runs on a schedule (configured in `default_config.yaml`); a manual run is exposed via the WebUI status badge.

## v2.5 Status

- v2.5 banner CTA changed from `open-plugin-config:skillopt` (dead) to `open-modal:/usr/plugins/skillopt/webui/config.html` (works).

## Verification

Run the optimizer manually, confirm a suggestion list appears in the WebUI. Approve one, confirm the new skill is callable from a chat.

## See also

- `plugin.yaml` — manifest (name, version, settings_sections, per_project_config, per_agent_config)
- `default_config.yaml` — defaults (referenced by `install()` and the WebUI settings UI)
- `README.md` — user-facing docs (what the plugin does from a user's perspective)
- Framework references: `helpers/plugins.py` (lifecycle), `helpers/api.py` (API dispatch), `helpers/ui_server.py` (asset serving)
