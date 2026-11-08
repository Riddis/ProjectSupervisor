<!--
This is G7, the contribution review (docs/BOUNDARY.md). Fill in every section before opening the pull request;
the maintainers rerun every gate and review regardless of what you report here (CONTRIBUTING.md, "Review: you
first, then the maintainers").
-->

## What this changes, and where it lives

<!-- One or two sentences: what the change is, and which layer it belongs to (framework, a plugin package, a
project) per docs/BOUNDARY.md. -->

## Would a different project want this?

<!-- Rule 1's written answer. If the change is a plugin package or project-only, say so and why it is not a
framework change instead. -->

## If this adds a new hook: its second user

<!-- Rule 3: a hook request names a generic use beyond the project asking for it. Name the second user here, or
write "not applicable, no new hook". -->

## Impact

- [ ] API impact (plugin API, `docs/PLUGIN_API.md`) — ticked and described, or marked not applicable
- [ ] Migration impact (`supervisor migrate`) — ticked and described, or marked not applicable
- [ ] Docs impact — ticked and described, or marked not applicable

## Gates run

<!-- Name every gate that applies to this change (CONTRIBUTING.md, "Which gates apply") and paste its output. -->

| Gate | Run? | Output |
|---|---|---|
| G1 boundary check | | |
| G2 plugin API contract | | |
| G3 framework suite | | |
| G4 release (release pull requests only) | | |
| G5 plugin conformance (plugin packages only) | | |
| G6 project compatibility (plugin packages only) | | |
| G7 contribution review | this checklist | |

## Plugin-package home

<!-- If this change adds or updates a plugin package shared through the catalog index, name its home: either
`packages/<distribution>` (kept in this repository) or the `https://` URL of its own repository. Write "not
applicable" if this change adds no plugin package. -->
