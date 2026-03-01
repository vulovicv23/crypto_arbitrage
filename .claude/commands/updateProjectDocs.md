---
allowed-tools: Bash(git diff:*), Bash(git status:*), Read, Glob, Grep, Write, Edit, Task
description: Update docs for a specific module
---

# Update Project Docs

Update documentation for a specific source module based on code changes. Does NOT commit or push.

## Arguments

**$ARGUMENTS** — The module name. Must be one of the valid modules listed below.

## Step 1: Validate Module

Verify the argument is a valid module name. If not, list valid options and stop.

Valid modules and their documentation mapping:

| Module | Source File | Docs File |
|--------|-------------|-----------|
| `models` | `src/models.py` | `docs/models.md` |
| `polymarket_client` | `src/polymarket_client.py` | `docs/polymarket_client.md` |
| `prediction_sources` | `src/prediction_sources.py` | `docs/prediction_sources.md` |
| `strategy` | `src/strategy.py` | `docs/strategy.md` |
| `risk` | `src/risk_manager.py` | `docs/risk.md` |
| `order_manager` | `src/order_manager.py` | `docs/order_manager.md` |
| `config` | `config.py` | `docs/config.md` |

## Step 2: Filter Changes

```bash
git diff --name-only -- {source_file}
git diff --cached --name-only -- {source_file}
```

If no changes found for this module, report "No changes detected for {module}" and stop.

## Step 3: Explore (Multi-Agent)

Launch two parallel Explore agents:

**Agent 1: Analyze Code Changes**
```
Task(
  subagent_type="Explore",
  description="Analyze {module} code changes",
  prompt="Thoroughness: very thorough

Analyze the code changes in {source_file} to understand what was added, modified, or removed.

RUN:
git diff HEAD -- {source_file}

FOR EACH CHANGE:
1. What functions/classes/methods were affected
2. What is the purpose of the change
3. Changed function signatures

RETURN structured summary:
- **Functions/Classes Affected**: With brief description
- **New Code**: Anything added from scratch
- **Removed Code**: Anything deleted
- **Impact**: What this change affects"
)
```

**Agent 2: Read Current Documentation State**
```
Task(
  subagent_type="Explore",
  description="Check {module} docs state",
  prompt="Thoroughness: medium

Read the current documentation file for {module}.

READ:
- {docs_file}

RETURN:
- **Current Content**: Brief summary of what the docs cover
- **Gaps**: Anything obviously missing or outdated"
)
```

## Step 4: Update Documentation

Using findings from both Explore agents, update the docs file:
- If new functions/classes were implemented, add them with signatures and behavior
- If code was modified, update docs to reflect new behavior
- If code was deleted, remove from docs
- Match existing documentation style

## Step 5: Report

```markdown
## Documentation Update: {module}

### File Modified
| File | Changes |
|------|---------|
| `{docs_file}` | {what was updated} |

**Not committed.** Review changes and run `/syncDocsAndCommit` when ready.
```
