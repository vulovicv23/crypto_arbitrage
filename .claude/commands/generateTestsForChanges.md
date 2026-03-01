---
allowed-tools: Read, Edit, Write, Glob, Grep, Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(pytest:*), Bash(python:*), Task
description: Analyze code changes and add/update tests
---

# Test Generator (Multi-Agent)

You are a test generation assistant using a **multi-agent exploration pattern**. Your job is to analyze code changes in the crypto_arbitrage bot, then add or update tests based on actual patterns discovered in the codebase.

## Why Multi-Agent?

Single-pass test generation often misses existing test patterns, fixtures, conftest helpers, and utilities. By spawning Explore agents first, you discover the actual test infrastructure and patterns used, resulting in tests that match the codebase style perfectly.

## Current State

**Branch:** !`git branch --show-current`

**Staged changes:**
!`git diff --cached --name-only`

**Unstaged changes:**
!`git diff --name-only`

## Test Structure

| Source | Test Location |
|--------|--------------|
| `src/models.py` | `tests/test_models.py` |
| `src/strategy.py` | `tests/test_strategy.py` |
| `src/risk_manager.py` | `tests/test_risk_manager.py` |
| `src/order_manager.py` | `tests/test_order_manager.py` |
| `src/polymarket_client.py` | `tests/test_polymarket_client.py` |
| `src/prediction_sources.py` | `tests/test_prediction_sources.py` |
| `config.py` | `tests/test_config.py` |
| `main.py` | `tests/test_main.py` |

## Step 1: Identify Changed Source Files

From the git status above, identify which source files were modified:
- `src/*.py` files
- `config.py`
- `main.py`

Ignore changes to: docs/, tests/, .claude/, *.md, .env*

## Step 2: Explore Test Patterns (Multi-Agent)

Launch TWO parallel Explore agents:

### Agent 1: Analyze Code Changes in Detail

```
Task(
  subagent_type="Explore",
  description="Analyze code changes for test generation",
  prompt="Thoroughness: very thorough

Analyze what was changed in the source files that need testing.

RUN: git diff HEAD -- src/ config.py main.py

FOR EACH CHANGED FUNCTION/CLASS:
1. Function signature (parameters and return type)
2. What logic branches exist (if/else, try/except)
3. What edge cases should be tested
4. What external dependencies need mocking (aiohttp, time, etc.)

RETURN structured list of testable changes:
- Function name
- Test scenarios (happy path, edge cases, error cases)
- Mock requirements"
)
```

### Agent 2: Discover Existing Test Patterns

```
Task(
  subagent_type="Explore",
  description="Discover existing test patterns",
  prompt="Thoroughness: very thorough

Read ALL existing test files in tests/.

FOR EACH TEST FILE:
1. Test naming convention
2. Fixtures used (and their source)
3. Mock patterns (what's mocked, how)
4. Assert patterns
5. Async test patterns (if any)

ALSO READ:
- tests/conftest.py (if exists)

RETURN:
- **Naming Convention**: how tests are named
- **Import Pattern**: standard imports used
- **Fixtures**: available fixtures with descriptions
- **Mock Patterns**: how external services are mocked
- **Assert Style**: assert statements vs pytest.raises vs other"
)
```

## Step 3: Generate Tests

Using findings from both agents, write tests that:

1. **Match existing patterns** — Use the same naming, fixtures, and mock patterns
2. **Cover all scenarios** — Happy path, edge cases, error cases
3. **Mock external I/O** — aiohttp sessions, WebSocket connections, time.time_ns
4. **Use async where needed** — For async functions, use `@pytest.mark.asyncio`
5. **Are focused** — One test per scenario, clear test names

### Test Naming Convention

```python
def test_{function_name}_{scenario}():
    """What this test verifies."""
    ...

# Examples:
def test_check_signal_approves_valid_signal():
def test_check_signal_rejects_when_halted():
def test_compute_edge_returns_none_for_zero_price():
```

## Step 4: Run Tests

```bash
pytest tests/ -v --tb=short
```

Fix any failures. If tests pass, report success.

## Step 5: Report

```markdown
## Test Generation Summary

### Tests Added
| File | Tests Added | Source Coverage |
|------|-------------|----------------|
| tests/test_strategy.py | 5 | _compute_edge, _classify_strength |
| tests/test_risk_manager.py | 8 | check_signal (all 9 gates) |

### Test Results
- **Passed**: {X}
- **Failed**: {X}
- **Errors**: {X}

**Not committed.** Review and run `/syncDocsAndCommit` when ready.
```
