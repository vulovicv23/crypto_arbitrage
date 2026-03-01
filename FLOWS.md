# Command Workflows

This document describes the standard workflows for developing the Crypto Arbitrage Bot using Claude Code commands.

## Flow 1: Daily Development

Use `/syncDocsAndCommit` after making changes:

```
1. Make code changes
2. Run /syncDocsAndCommit
   ├── Formats code (black + ruff)
   ├── Identifies changed modules
   ├── Launches Explore agents to analyze changes
   ├── Updates documentation for affected modules
   ├── Creates conventional commit
   └── Pushes to remote
```

This is the **only** way to commit and push changes. Direct `git commit` / `git push` is blocked by hooks.

## Flow 2: Building Features

For larger features, use the three-step workflow:

```
1. /createFeatureTasks <feature description>
   ├── Asks clarifying questions
   ├── Explores codebase for relevant context
   ├── Creates task specification files in .tasks/
   └── Returns list of task files to execute

2. /executeTask <task-file-path>   (for each task)
   ├── Reads the task specification
   ├── Launches Explore agents for deep research
   ├── Implements the task
   ├── Runs tests to verify
   └── Reports completion

3. /syncDocsAndCommit
   ├── Formats and lints
   ├── Updates all affected docs
   └── Commits and pushes
```

## Flow 3: Debugging

When something isn't working:

```
1. /debug <description of the issue>
   ├── Analyzes code for potential causes
   ├── Runs the bot if needed
   ├── Uses Playwright to inspect browser state (if dashboard)
   ├── Checks logs for errors
   └── Proposes and implements fixes

2. /syncDocsAndCommit   (if changes were made)
```

## Flow 4: Documentation Only

When you want to update docs without committing:

```
1. /updateProjectDocs <module>
   ├── Analyzes code changes for one module
   ├── Updates the module's docs file
   └── Reports what was changed (no commit)

   OR

2. /documentChangedProjects
   ├── Finds all changed source files
   ├── Updates docs for every affected module
   └── Reports all changes (no commit)

3. /syncDocsAndCommit   (when ready to commit)
```

## Flow 5: Test Coverage

Check and improve test coverage:

```
1. /auditProjectTests
   ├── Analyzes existing test files
   ├── Identifies missing coverage
   └── Reports gaps and recommendations

2. /generateTestsForChanges
   ├── Analyzes recent code changes
   ├── Generates tests for new/modified code
   └── Runs tests to verify

3. /syncDocsAndCommit
```

## Flow 6: Documentation Audit

Deep audit of documentation quality:

```
1. /auditProjectDocs
   ├── Reads all source code
   ├── Reads all documentation
   ├── Identifies gaps, inaccuracies, and missing docs
   ├── Fixes all issues
   └── Reports changes

2. /syncDocsAndCommit
```

## Command Quick Reference

| Command | Purpose | Commits? |
|---------|---------|----------|
| `/runInfra` | Start PostgreSQL | No |
| `/runArbBot` | Run the bot | No |
| `/syncDocsAndCommit` | Format + docs + commit + push | **Yes** |
| `/updateProjectDocs <module>` | Update docs for one module | No |
| `/documentChangedProjects` | Update docs for all changes | No |
| `/createFeatureTasks` | Plan a feature | No |
| `/executeTask <file>` | Execute a task spec | No |
| `/debug` | Debug an issue | No |
| `/generateTestsForChanges` | Generate tests | No |
| `/auditProjectDocs` | Audit documentation | No |
| `/auditProjectTests` | Audit test coverage | No |
