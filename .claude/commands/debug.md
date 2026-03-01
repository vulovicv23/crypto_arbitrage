---
allowed-tools: Read, Edit, Write, Glob, Grep, Bash(python:*), Bash(docker compose:*), Bash(docker:*), Bash(curl:*), Bash(ls:*), Task
description: Debug an issue by analyzing code and running the bot
---

# Debug Issue (Multi-Agent)

You are a debugging assistant using a **multi-agent exploration pattern**. Your job is to diagnose and fix issues by analyzing code and inspecting runtime behavior.

## Why Multi-Agent?

Debugging requires understanding multiple layers: code structure, runtime behavior, log output, and data flow. By spawning Explore agents for code analysis while inspecting logs and runtime state, you can quickly identify root causes.

## Issue Context

**User's Description:** $ARGUMENTS

## Your Task

### Step 1: Understand the Issue

If `$ARGUMENTS` is empty or unclear, ask the user:

```
To help debug, please provide:

1. **Which module?** (strategy, risk, order_manager, polymarket_client, prediction_sources, or combination)
2. **What's the error?** (Error message, traceback, unexpected behavior, or symptom)
3. **When does it occur?** (On startup, during trading, specific market condition, etc.)
4. **Steps to reproduce** (if known)
```

### Step 2: Multi-Agent Code Analysis

Based on the issue, launch targeted Explore agents:

**Agent 1: Analyze Affected Code**
```
Task(
  subagent_type="Explore",
  description="Analyze code for debugging",
  prompt="Thoroughness: very thorough

Investigate the {affected_area} code for potential issues related to: {issue_description}

READ the relevant source files and look for:
1. Logic errors or edge cases
2. Missing error handling
3. Race conditions in async code
4. Incorrect assumptions about data
5. Missing null/empty checks

RETURN:
- **Potential Causes**: ranked by likelihood
- **Code Locations**: exact file:line references
- **Suggested Fixes**: for each potential cause"
)
```

**Agent 2: Check Related Code**
```
Task(
  subagent_type="Explore",
  description="Check related code paths",
  prompt="Thoroughness: medium

Check code that interacts with the affected area.
Look for:
1. Callers that might pass unexpected data
2. Configuration that might be wrong
3. Queue interactions that might cause issues
4. Timing/ordering dependencies

RETURN:
- **Related Code**: that might contribute to the issue
- **Data Flow**: how data reaches the affected area
- **Configuration**: relevant settings and defaults"
)
```

### Step 3: Runtime Investigation (if needed)

If the issue requires running the bot:

```bash
python main.py --dry-run
```

Check logs:
- `logs/bot.log` for general errors
- `logs/trades.jsonl` for trade-related issues
- Console output for real-time diagnostics

### Step 4: Diagnose

Based on code analysis and runtime investigation:
1. Identify the root cause
2. Explain why it happens
3. Propose a fix

### Step 5: Fix

If the fix is straightforward:
1. Implement the fix
2. Run the bot in dry-run mode to verify
3. Run tests if they exist

If the fix is complex:
1. Explain the proposed solution
2. Ask for user approval before implementing

### Step 6: Report

```markdown
## Debug Report

### Issue
{Description of the issue}

### Root Cause
{What's actually wrong and why}

### Fix Applied
| File | Change |
|------|--------|
| `src/strategy.py:142` | {what was fixed} |

### Verification
- {How the fix was verified}

**Not committed.** Review and run `/syncDocsAndCommit` when ready.
```
