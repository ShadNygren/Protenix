---
name: Memory Location Preference
description: Memory files must be stored in the project repo directory and symlinked from ~/.claude, not stored only in ~/.claude
type: feedback
---

Memory files must live in the project's Git repo (e.g., `memory/` directory) so they are version-controlled and backed up, not stored solely under `~/.claude/` which is tied to a specific machine.

**Why:** If the local NVMe SSD crashes or is corrupted, any data only in `~/.claude/` would be lost. Memory is important context that should be committed to GitHub.

**How to apply:** For each project, create a `memory/` directory in the repo and symlink `/home/dell/.claude/projects/<project-path>/memory/` to it. This pattern has been used across multiple projects.
