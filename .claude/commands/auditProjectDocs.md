---
allowed-tools: Read, Glob, Grep, Write, Edit, Task
description: Thoroughly audit and fix documentation for a specific module
---

# Audit Project Docs

Comprehensive audit of documentation. Inventories ALL source code, cross-references against docs, identifies gaps, and fixes them.

## Arguments

**$ARGUMENTS** — Optional module name. If provided, audit only that module. If empty, audit all documentation.

Valid modules: `models`, `polymarket_client`, `prediction_sources`, `strategy`, `risk`, `order_manager`, `config`

## Step 1: Deep Inventory (Multi-Agent)

Launch Explore agents to inventory ALL source code. **Launch ALL agents in a SINGLE message for parallel execution.**

### Agent 1: Inventory Source Code

```
Task(
  subagent_type="Explore",
  description="Inventory all source code",
  prompt="Thoroughness: very thorough

Inventory ALL Python source files in the project.

FOR EACH FILE in src/, config.py, main.py:
1. List all classes with their methods (name + signature)
2. List all standalone functions (name + signature)
3. List all constants
4. List all dataclass fields
5. Note any complex logic that needs documentation

RETURN as structured inventory:
- **File**: path
  - **Classes**: name, methods with signatures
  - **Functions**: name with signature
  - **Constants**: name and value
  - **Complexity Notes**: anything non-obvious"
)
```

### Agent 2: Inventory Current Documentation

```
Task(
  subagent_type="Explore",
  description="Inventory all documentation",
  prompt="Thoroughness: very thorough

Read ALL documentation files in the project.

READ:
- CLAUDE.md
- README.md
- FLOWS.md
- docs/README.md
- docs/*.md (every file)

FOR EACH DOC FILE:
1. What classes/functions/concepts does it document?
2. What signatures or parameter tables does it include?
3. What's missing compared to what you'd expect?

RETURN:
- **File**: path
  - **Documents**: list of classes/functions covered
  - **Missing**: anything not documented
  - **Outdated**: anything that looks stale"
)
```

## Step 2: Cross-Reference

Compare source inventory against documentation inventory:

1. **Missing docs**: Source code exists but no docs entry
2. **Stale docs**: Docs describe something that no longer exists or has changed
3. **Incomplete docs**: Docs exist but are missing parameters, return types, or behavior details
4. **Incorrect docs**: Docs describe wrong behavior

## Step 3: Fix All Issues

For each gap found:
- Add missing documentation following the existing style
- Update stale or incorrect entries
- Add missing parameter tables, return types, and examples
- Ensure consistency across all doc files

## Step 4: Update Documentation Index

If new documentation was added, update `docs/README.md` to include it.

## Step 5: Report

```markdown
## Documentation Audit Report

### Summary
- **Files Audited**: {count}
- **Issues Found**: {count}
- **Issues Fixed**: {count}

### Changes Made
| File | Change |
|------|--------|
| `docs/strategy.md` | Added _classify_strength method docs |
| `docs/risk.md` | Updated position sizing formula |

### Remaining Issues
- {any issues that couldn't be auto-fixed}

**Not committed.** Review changes and run `/syncDocsAndCommit` when ready.
```
