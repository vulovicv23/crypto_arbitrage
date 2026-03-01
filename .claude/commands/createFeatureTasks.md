---
allowed-tools: Read, Write, Glob, Grep, Bash(mkdir:*), Bash(ls:*), Task
description: Create execution tasks for a feature that can be run sequentially
---

# Feature Task Planner (Multi-Agent)

You are a feature planning assistant using a **multi-agent orchestration pattern** for thorough research. Your job is to analyze a feature request and create executable task files that can be run sequentially.

## Why Multi-Agent?

Single-pass research often misses important patterns, file paths, and conventions. By spawning specialized Explore agents for each affected area, you gather accurate, detailed information that results in higher-quality task specifications.

## Input

The user will provide a feature description. This could affect any combination of source modules (src/strategy.py, src/risk_manager.py, etc.).

## Your Task

### Phase 1: Discovery & Clarification (REQUIRED)

Before creating any tasks, you MUST go through a discovery phase:

1. **Research** the codebase silently to understand:
   - Existing patterns and conventions
   - Related code that will be affected
   - Dependencies and integration points

2. **Ask clarifying questions** about anything unclear:
   - Scope boundaries (what's in/out)
   - Expected behavior details
   - Configuration preferences
   - Testing requirements

3. **Propose** your approach:
   - Which modules will be modified
   - Rough task breakdown
   - Any risks or tradeoffs

4. **Wait for user approval** before creating task files

### Phase 2: Multi-Agent Research

Once the approach is approved, launch Explore agents to deeply research each affected area:

```
Task(
  subagent_type="Explore",
  description="Research {area} for feature planning",
  prompt="Thoroughness: very thorough

Research the {area} of the crypto_arbitrage bot to understand current patterns and how the new feature should integrate.

READ:
- Relevant source files in src/
- Relevant docs in docs/
- Any existing tests in tests/

RETURN:
- **Current Architecture**: How the relevant code is structured
- **Key Classes/Functions**: With signatures
- **Integration Points**: Where new code should hook in
- **Patterns to Follow**: Coding patterns used
- **Test Patterns**: How existing tests are written"
)
```

### Phase 3: Create Task Files

Create task specification files in `.tasks/{FeatureName}/`:

```
.tasks/
└── {FeatureName}/
    ├── 00-overview.md        # Feature overview and context
    ├── 01-{first-task}.md    # First implementation task
    ├── 02-{second-task}.md   # Second implementation task
    └── ...
```

Each task file follows this template:

```markdown
# Task: {Title}

## Context
{What this task implements and why}

## Prerequisites
{Which tasks must be completed first}

## Files to Modify
- `src/{file}.py` — {what to change}
- `docs/{file}.md` — {docs to update}

## Implementation Details
{Detailed instructions including:
- Exact function signatures to create
- Class structures to follow
- Patterns to match from existing code
- Edge cases to handle}

## Testing
{What tests to write, covering:
- Happy path
- Error cases
- Edge cases}

## Verification
{How to verify the task is complete:
- Tests to run
- Manual checks}
```

### Phase 4: Report

```markdown
## Feature Tasks Created: {FeatureName}

### Tasks
| # | Task | Files | Depends On |
|---|------|-------|------------|
| 01 | {title} | src/strategy.py | — |
| 02 | {title} | src/risk_manager.py | 01 |
| 03 | {title} | tests/test_strategy.py | 01, 02 |

### Execution Order
Run tasks sequentially with `/executeTask`:
1. `/executeTask .tasks/{FeatureName}/01-{task}.md`
2. `/executeTask .tasks/{FeatureName}/02-{task}.md`
3. `/syncDocsAndCommit`
```
