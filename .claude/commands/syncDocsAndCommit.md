---
allowed-tools: Bash(black:*), Bash(ruff:*), Bash(git add:*), Bash(git commit:*), Bash(git push), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Read, Glob, Grep, Write, Edit, Task
description: Auto-fix, update docs, commit, and push
---

# Sync Docs and Commit

This is ran from automation script.

## Step 1: Auto-Fix Formatting and Linting

Run black and ruff to auto-fix all formatting and lint issues:

```bash
black .
ruff check --fix .
```

If either tool reports unfixable errors, note them for the commit message but continue.

## Step 2: Inspect Current State

```bash
git branch --show-current
git status
git diff --name-only
git diff --cached --name-only
```

Combine staged and unstaged changes into one list of affected files.

## Step 3: Identify Affected Modules

Map changed files to documentation:

| File Path | Docs to Update |
|---|---|
| `src/models.py` | `docs/models.md` |
| `src/polymarket_client.py` | `docs/polymarket_client.md` |
| `src/prediction_sources.py` | `docs/prediction_sources.md` |
| `src/strategy.py` | `docs/strategy.md` |
| `src/risk_manager.py` | `docs/risk.md` |
| `src/order_manager.py` | `docs/order_manager.md` |
| `config.py` | `docs/config.md` |
| `main.py` | `CLAUDE.md` (architecture section if changed) |
| `src/logger_setup.py` | (minor — only update if logging behavior changed) |

If only docs/config files changed, skip documentation update and go directly to Step 6.

## Step 4: Explore Changes (Multi-Agent)

For each affected source file, launch TWO parallel Explore agents:

### Agent 1: Analyze Code Changes

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

ALSO CHECK:
- New modules or files added
- Changed function signatures
- New dependencies or imports
- Changed configuration

RETURN structured summary:
- **Files Changed**: List with change type
- **Functions/Classes Affected**: With brief description
- **New Code**: Anything added from scratch
- **Removed Code**: Anything deleted
- **Impact**: What this change affects"
)
```

### Agent 2: Check Current Documentation State

```
Task(
  subagent_type="Explore",
  description="Check docs state",
  prompt="Thoroughness: medium

Read all documentation files for the project.

READ:
- docs/README.md
- docs/*.md (all files)
- CLAUDE.md

RETURN:
- **Existing Docs**: List of files with brief content summary
- **Gaps**: Anything obviously missing or outdated"
)
```

## Step 5: Update Documentation

Using findings from both Explore agents:

### 5a: Update docs/ files

For each affected source file, update the corresponding docs/ file:
- If a new function/class was implemented, add it to the docs with signature, parameters, and behavior
- If code was modified, update the docs to reflect new behavior
- If code was deleted, remove or mark as removed in docs
- Match existing documentation style in the file

### 5b: Update CLAUDE.md if needed

Only if architectural changes were made (new pipeline stages, changed data flow, new dependencies).

## Step 6: Stage and Commit

```bash
git add -A
```

Review what will be committed:
```bash
git status
```

Create a conventional commit message based on the changes:
- `feat:` for new features
- `fix:` for bug fixes
- `refactor:` for refactoring
- `docs:` for documentation-only changes
- `test:` for test-only changes
- `chore:` for maintenance

If multiple types, use the primary type and mention others in the body.

```bash
git commit -m "<type>: <concise summary>

<optional body with details>

Co-Authored-By: Claude <noreply@anthropic.com>"
```

## Step 7: Push

```bash
git push
```

If push fails (e.g., remote has new commits), pull first:
```bash
git pull --rebase && git push
```

## Output Format

```markdown
## Sync Summary

### Formatting
- black: {X files reformatted / All clean}
- ruff: {X issues fixed / All clean}

### Modules Updated
| Source File | Docs Updated |
|---|---|
| src/strategy.py | docs/strategy.md |
| src/risk_manager.py | docs/risk.md |

### Commit
- Branch: {branch}
- Message: {commit message}
- Hash: {short hash}
- Pushed: Yes/No
```

## Important Rules

1. **Always run black and ruff first** — formatting fixes should be part of the same commit
2. **Never modify code files** — this command only touches docs and formatting
3. **If no code changes exist** (only docs/config), skip Steps 4-5 and commit directly
4. **If push fails after rebase**, stop and inform the user
