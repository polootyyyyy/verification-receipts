# Verification receipts

Keep a local test result tied to the files it checked. After edits or a resumed
Claude Code session, ask whether the result is still current instead of relying on
a remembered “tests passed.”

This small Claude Code skill includes a dependency-free Python helper. It does not
add hooks, run a daemon, change settings, access a browser, or send data anywhere.
It complements your existing planning and execution workflow.

## Install

Requirements: Python 3.10 or newer, Git, and Linux or macOS with POSIX no-follow file
access. Keep this folder, including `scripts/`, together. For a project-local skill,
place it at `.claude/skills/verification-receipts/`; for a personal skill use
`~/.claude/skills/verification-receipts/`. Review the files before installation.
Claude Code can discover the skill normally or you can invoke `/verification-receipts`.
Installation does not run any command.

The official [skills guide](https://code.claude.com/docs/en/skills) describes these
locations. The [context management documentation](https://code.claude.com/docs/en/how-claude-code-works#context-management)
explains why a resumed session may need to reconstruct earlier details. Receipts
provide a bounded check of previous verification; they do not fix compaction.

## Use

Run from any directory inside the Git repository being checked. Substitute the
actual helper path. Capture runs a **newly authorized command** as separate argv:

```sh
python3 /path/to/verification-receipts/scripts/verification_receipts.py capture --timeout 300 -- python3 -m unittest discover -s tests
```

The last stdout line is JSON with `status`, `reason`, `receipt`, and
`scope_exclusions`. Command output is saved privately, rather than mixed into that
JSON. Keep the returned receipt path for a future check:

```sh
python3 /path/to/verification-receipts/scripts/verification_receipts.py status /absolute/path/to/receipt.json
python3 /path/to/verification-receipts/scripts/verification_receipts.py show /absolute/path/to/receipt.json
```

`show` includes timestamps, argv, return code, workspace hashes, and the relative
log reference. Neither inspection command executes stored argv or reads the log as
instructions. JSON escapes control characters in stored text.

| Status | Exit | Meaning |
| --- | --- | --- |
| `PASS_CURRENT` | 0 | Command passed; scoped workspace still matches |
| `FAIL_CURRENT` | 1 | Command failed or timed out; scoped workspace still matches |
| `STALE` | 2 | Scoped workspace changed after capture |
| `INVALID` | 3 | Evidence is malformed, mismatched, altered, or captured across edits |
| `UNKNOWN` | 4 | Command could not start, output exceeded its limit, or state was unverifiable |

Argument/usage errors use argparse's exit 2 and diagnostic stderr instead of a status
record. A capture stopped before execution may return a null receipt path.

Timeout defaults to 300 seconds and must be in `(0, 3600]`. Output is limited to
16 MiB. On timeout, output overflow, or normal completion, remaining children in the
command's original process group are killed. Processes that deliberately detach to
another session escape this cleanup; the helper is not an execution sandbox. Apply
your normal command review and sandbox policy before capture.

## What is checked

Before and after capture, and again on status, the helper hashes:

- HEAD and staged index entries, including staged contents and executable modes.
- Raw tracked worktree and non-ignored untracked file contents and permission modes.
- Deletions and symlink target text, without dereferencing links.

Git inventories use NUL-separated paths, so spaces, tabs, and newlines in filenames
are supported. A directory replaced by a symlink is not followed to read children.
Unmerged index entries, submodules, special files, unsafe storage, and unsupported
platforms fail closed. Inspection stops at 100,000 directory entries, depth 64,
or a source file larger than 64 MiB;
ignored directories are pruned. A successful command that changes captured source is
`INVALID`, even if it exits zero. Worktree changes that happen and are restored
entirely between observations cannot be detected.

**Scope is deliberately limited.** Ignored files, empty directories, executable
versions, environment variables, dependencies, services, external files, and symlink
referent contents are not verified. For example, changing an ignored build output
does not make a receipt stale. A passing receipt cannot establish that an ignored
configuration or a dependency was unchanged. Review the exclusions with every result
and rerun appropriate checks whenever such inputs matter.

## Storage and privacy

Artifacts live in `verification-receipts/` under Git's resolved metadata directory,
including linked-worktree metadata. They stay outside source inventories. The
directory is mode `0700`; JSON receipts and combined stdout/stderr logs are mode
`0600`. Writes use temporary files and atomic replacement. Inspection accepts only a
receipt belonging directly to the current repository's private storage, rejects
symlink artifact files and traversal, and verifies the log's SHA-256 digest.

Do not put secrets in command arguments or output: both are retained locally.
Environment variables are not dumped, but the command inherits its usual environment
and can print it. Nothing is redacted automatically. Inspect local files carefully
before sharing and remove receipts/logs you no longer need. They are not committed
or uploaded by this tool.

Receipts provide local integrity checks, **not signed attestations**. Anyone who can
rewrite the receipt and log can forge matching hashes. A command can lie, modify
state between snapshots, or access files outside the measured scope. The result
means that the recorded command exited as shown against the observed scoped state;
it does not prove test quality, repository safety, or release readiness.

## Development

Run the integration suite in an isolated disposable environment:

```sh
python3 -m unittest discover -s tests -v
```

Tests create temporary Git repositories and exercise actual capture/status/show
behavior. The public helper and tests use only Python's standard library and Git.

MIT licensed. See [LICENSE](LICENSE).
