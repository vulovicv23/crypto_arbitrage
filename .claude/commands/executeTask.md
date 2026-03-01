---
allowed-tools: Read, Edit, Write, Glob, Grep, Bash(pytest:*), Bash(python:*), Bash(git status:*), Bash(git diff:*), Bash(ls:*), Task
description: Execute a task specification file with multi-agent exploration for thorough implementation
---

# Execute Task (Multi-Agent)

You are a task execution assistant using a **multi-agent exploration pattern**. Your job is to implement a task specification file thoroughly and correctly by first deeply understanding the codebase patterns.

## Why Multi-Agent?

Task specs describe WHAT to implement, but not the exact HOW. By spawning Explore agents before implementation, you discover the actual patterns, conventions, and code style used in the codebase, resulting in implementations that perfectly match existing code.

## Target Task

**Task File:** $ARGUMENTS

## Usage

```
/executeTask .tasks/FeatureName/01-task-name.md
```

## Your Task

### Step 1: Validate and Read Task File

If `$ARGUMENTS` is empty:
- List `.tasks/` directory contents
- Ask user which task to execute
- Stop and wait

Read the task file completely. Extract:
- **Context**: What this implements
- **Prerequisites**: What must be done first
- **Files to Modify**: Which files are affected
- **Implementation Details**: What to build
- **Testing**: What tests to write
- **Verification**: How to verify success

### Step 2: Pre-Implementation Research (Multi-Agent)

Launch Explore agents to understand the code you'll be modifying:

```
Task(
  subagent_type="Explore",
  description="Research code for task implementation",
  prompt="Thoroughness: very thorough

Read the source files that will be modified for this task:
{list files from task spec}

FOR EACH FILE:
1. Full file structure (classes, methods, imports)
2. Coding patterns used (naming, error handling, logging)
3. How similar functionality is implemented elsewhere
4. Import conventions and dependencies

ALSO READ related docs:
{list relevant docs files}

RETURN:
- **Code Structure**: How each file is organized
- **Patterns**: Coding conventions to follow
- **Integration Points**: Where new code hooks in
- **Dependencies**: What needs to be imported"
)
```

### Step 3: Implement

Follow the task specification exactly. For each change:
1. Read the target file
2. Identify the insertion/modification point
3. Write code matching existing patterns
4. Verify imports are correct

### Step 4: Write Tests

If the task spec includes testing requirements:
1. Create or update test files
2. Follow existing test patterns
3. Cover happy path, edge cases, error cases
4. Use appropriate mocking for I/O

### Step 5: Verify

```bash
pytest tests/ -v --tb=short
```

If tests fail, fix and re-run until green.

### Step 6: Report

```markdown
## Task Completed: {task title}

### Changes Made
| File | Change |
|------|--------|
| `src/strategy.py` | Added {description} |
| `tests/test_strategy.py` | Added 5 tests |

### Tests
- **Passed**: {X}
- **Failed**: 0

### Next Task
{Suggest the next task file to execute, if applicable}

**Not committed.** Run `/syncDocsAndCommit` when ready.
```
