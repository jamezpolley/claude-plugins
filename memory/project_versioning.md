---
name: Plugin versioning decision
description: Why version fields were removed from plugin.json files, and when to reconsider
type: project
---

Removed explicit `version` fields from all `plugin.json` files so that git commit SHA is used instead. Every commit is automatically a new version — no manual bumping required.

**Why:** Personal single-developer repo; forgetting to bump version would silently strand users on old copies. SHA approach is zero-maintenance and correct for this scale.

**How to apply:** If the marketplace grows (more plugins, external contributors, or users who only install a subset of plugins), revisit adding explicit versions. The main pain point at scale is that a monorepo SHA changes for all plugins on every commit, triggering spurious re-downloads for plugins that didn't actually change. At that point, per-plugin explicit versions (or splitting plugins into separate repos) become worth the overhead.
