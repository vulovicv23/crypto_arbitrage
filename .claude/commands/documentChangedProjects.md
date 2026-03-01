---
allowed-tools: Bash(git diff:*), Bash(git status:*), Read, Glob, Grep, Write, Edit, Task
description: Update docs for all changed modules
---

# Document Changed Modules

Analyze all code changes and update documentation for every affected module. Does NOT commit or push.

## Step 1: Identify Affected Modules

```bash
git diff --name-only
git diff --cached --name-only
```

Map changed files to documentation:

| File Path | Module | Docs to Update |
|---|---|---|
| `src/models.py` | models | `docs/models.md` |
| `src/polymarket_client.py` | polymarket_client | `docs/polymarket_client.md` |
| `src/prediction_sources.py` | prediction_sources | `docs/prediction_sources.md` |
| `src/strategy.py` | strategy | `docs/strategy.md` |
| `src/risk_manager.py` | risk | `docs/risk.md` |
| `src/order_manager.py` | order_manager | `docs/order_manager.md` |
| `config.py` | config | `docs/config.md` |
| `main.py` | main | `CLAUDE.md` (architecture only) |

If only docs/config files changed, report "No code changes detected — documentation is already up to date" and stop.

## Step 2: For Each Affected Module

### 2a: Launch Explore Agents (Parallel)

For ALL affected modules, launch agents simultaneously:

**Agent 1: Analyze Code Changes**
```
Task(
  subagent_type="Explore",
  description="Analyze code changes",
  prompt="Thoroughness: very thorough

Analyze the code changes to understand what was added, modified, or removed.

RUN:
git diff HEAD -- src/ config.py main.py

FOR EACH CHANGED FILE:
1. File path and change type (added/modified/deleted)
2. What functions/classes/methods were affected
3. What is the purpose of the change

RETURN structured summary:
- **Files Changed**: List with change type
- **Functions/Classes Affected**: With brief description
- **New Code**: Anything added from scratch
- **Removed Code**: Anything deleted
- **Impact**: What this change affects"
)
```

**Agent 2: Check Current Documentation State**
```
Task(
  subagent_type="Explore",
  description="Check docs state",
  prompt="Thoroughness: medium

Read all documentation files.

READ:
- docs/README.md
- docs/*.md (all files)
- CLAUDE.md

RETURN:
- **Existing Docs**: List of files with brief content summary
- **Gaps**: Anything obviously missing or outdated"
)
```

### 2b: Update Documentation

For each affected module, update the corresponding docs/ file:
- If new functions/classes were implemented, add them with signatures and behavior
- If code was modified, update docs to reflect new behavior
- If code was deleted, remove from docs
- Match existing documentation style

## Step 3: Report

```markdown
## Documentation Update Summary

| Source File | Docs Updated |
|---|---|
| src/strategy.py | docs/strategy.md |
| src/risk_manager.py | docs/risk.md |
| config.py | docs/config.md |

### Files Modified
- docs/strategy.md — added new regime detection logic
- docs/risk.md — updated position sizing formula
- docs/config.md — added new env var documentation

**Not committed.** Review changes and run `/syncDocsAndCommit` when ready.
```
