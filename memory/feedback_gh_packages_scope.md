---
name: gh CLI packages scope keeps breaking
description: gh auth login drops read:packages and write:packages scopes on every re-auth — must always check and fix proactively
type: feedback
---

The `gh` CLI token repeatedly loses `read:packages` and `write:packages` scopes because `gh auth login` only requests default scopes (`repo`, `read:org`, `gist`). Every re-authentication silently drops the package scopes.

**Why:** This is a design flaw in `gh` — it doesn't persist custom scopes across re-authentication. The token partially works (repo operations succeed) but GHCR/package API calls fail with 403. This has caused repeated frustration.

**How to apply:**
1. At the START of any session that might touch Docker images or GHCR, run `gh auth status` and check for `write:packages` AND `delete:packages` in the scopes
2. If missing, immediately tell the user to run: `! gh auth refresh -h github.com -s read:packages,write:packages,delete:packages`
3. Always request ALL THREE package scopes at once — read, write, AND delete — because GitHub treats them as separate permissions and each requires its own scope
4. Do NOT attempt GHCR API calls without first verifying the scopes are present
5. Never suggest "just create a new key with permissions" — that's been tried repeatedly and doesn't stick
