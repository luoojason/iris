# Repo workspaces and ARTIFACT hand-back

Date: 2026-06-09
Status: approved

## Problem

A job that audits or builds something needs a directory to work in. Letting
the model name directories is how a prompt injection walks the filesystem.
And a job that produces files (a report, a clip, a patch) needs a way to hand
them to the owner without getting a "send anything anywhere" tool.

## Names, not paths

`IRIS_WORKSPACES_FILE` (default `iris-workspaces.json`) holds a registry the
**owner** edits via CLI only:

```json
{"myrepo": "/home/you/myrepo", "vault": "/home/you/notes"}
```

- `iris workspaces add <name> <path>` — name must match
  `[a-z0-9][a-z0-9-_]*` (max 32 chars); the path must exist and be a
  directory; it is stored resolved (`realpath`).
- `iris workspaces remove <name>`
- `iris workspaces list`

The model only ever says `workspace="myrepo"`. Resolution happens in the
job runner; the MCP layer rejects anything that is not a registered name.
There is no MCP tool that adds or removes workspaces, and no tool output
ever includes the underlying path.

A job with a workspace gets it as `--add-dir`, and its `files`/`shell`
grants (if given, and if within the `IRIS_JOB_GRANTS` ceiling) operate
there. A job without a workspace gets no `--add-dir` beyond the attachments
dir behavior chat already has.

## ARTIFACT hand-back

A job's report may contain lines of the form:

```
ARTIFACT: relative/path/inside/workspace.ext
```

After the job turn finishes, the runner scans the report for these lines and
collects the named files:

- Only workspace-relative paths. Absolute paths, `..` segments, and paths
  whose `realpath` escapes the workspace are rejected (symlinks cannot smuggle
  a file out).
- Caps: at most **5 files**, **8 MB total**. Past either cap, the remaining
  artifacts are skipped and the skips are named in the fold-back report —
  never silently dropped.
- Each surviving file is uploaded to the owner's recorded home channel via
  Discord REST multipart (the same transport the TTS server uses), with the
  job id in the message. Upload failures are reported per file, not fatal to
  the job.

A job with no workspace cannot hand back artifacts (there is nothing to be
relative to); `ARTIFACT:` lines in its report are reported as unresolvable.

## Invariants

- The registry is owner-authored; the model can neither create nor inspect
  paths.
- Artifact resolution is containment-checked with `realpath` on both sides.
- The caps are enforced before any byte is read for upload, and every skip is
  visible to the owner.
