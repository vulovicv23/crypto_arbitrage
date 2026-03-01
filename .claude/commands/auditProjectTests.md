---
allowed-tools: Read, Glob, Grep, Task
description: Audit test coverage for a specific module
---

# Audit Project Tests

Audit test coverage for the project. Inventories source modules and test files, identifies gaps, and produces a prioritized report. Does NOT generate tests — use `/generateTestsForChanges` for that.

## Arguments

**$ARGUMENTS** — Optional module name to audit. If empty, audit all modules.

Valid modules: `models`, `polymarket_client`, `prediction_sources`, `strategy`, `risk`, `order_manager`, `config`

## Step 1: Inventory Source and Test Code (Multi-Agent)

Launch TWO parallel Explore agents:

**Agent 1: Inventory Source Code**
```
Task(
  subagent_type="Explore",
  description="Inventory source code for test audit",
  prompt="Thoroughness: very thorough

Inventory ALL Python source files in src/, config.py, and main.py.

FOR EACH FILE:
1. List ALL classes with ALL methods (include private methods)
2. List ALL standalone functions
3. For each function/method: note parameter count, complexity, and whether it has branching logic
4. Note any error handling paths (try/except, raise)

Focus on testable units:
- Public methods and functions
- Complex private methods
- Error handling paths
- Edge cases (empty inputs, boundary conditions)

RETURN structured inventory grouped by file:
- File path
  - Testable units with complexity rating (low/medium/high)"
)
```

**Agent 2: Inventory Existing Tests**
```
Task(
  subagent_type="Explore",
  description="Inventory existing tests",
  prompt="Thoroughness: very thorough

Inventory ALL test files in tests/.

FOR EACH TEST FILE:
1. List all test functions/methods
2. What source function/class does each test cover?
3. What test patterns are used? (fixtures, mocks, parametrize)
4. Are there any shared fixtures in conftest.py?

RETURN:
- **Test Files**: list with test function names
- **Coverage Map**: which source functions have tests
- **Patterns**: testing conventions used
- **Fixtures**: shared fixtures available"
)
```

## Step 2: Gap Analysis

Cross-reference source inventory against test inventory:

### Priority Levels

| Priority | Criteria |
|----------|----------|
| **P0 - Critical** | Public API methods with no tests, error handling paths untested |
| **P1 - High** | Complex methods (high branching) with no tests |
| **P2 - Medium** | Simple methods with no tests, edge cases not covered |
| **P3 - Low** | Private helpers, simple getters/setters |

## Step 3: Report

```markdown
## Test Coverage Audit

### Summary
| Module | Source Functions | Tested | Untested | Coverage |
|--------|----------------|--------|----------|----------|
| strategy | 8 | 3 | 5 | 37% |
| risk_manager | 6 | 0 | 6 | 0% |

### P0 - Critical Gaps
- `RiskManager.check_signal()` — 9 risk checks, zero tests
- `OrderManager._submit_order()` — submission + error path untested

### P1 - High Priority
- `StrategyEngine._compute_edge()` — edge computation with confidence scaling
- `PredictionAggregator._extrapolate()` — linear regression logic

### Recommendations
1. Start with P0 items — run `/generateTestsForChanges` after adding test files
2. Use existing patterns: {describe patterns found}
```
