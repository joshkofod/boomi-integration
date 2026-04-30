# Boomi Integration — Tuned Fork

Forked from [OfficialBoomi/boomi-integration](https://github.com/OfficialBoomi/boomi-integration) for team use with Gemini CLI and custom tooling.

## What's Custom

- Canvas arranger script (standalone, replaces Claude Code agent)
- Team-specific tuning for Gemini CLI workflows
- (Add more as you customize)

## Staying Up To Date

Pull upstream changes:
```bash
git fetch upstream
git merge upstream/main
```

Or use GitHub's sync:
```bash
gh repo sync nexus9888/boomi-integration -b main
```

## Installation (Team Members)

```bash
git clone https://github.com/nexus9888/boomi-integration.git
# Point your agent's skill path to the cloned directory
```
