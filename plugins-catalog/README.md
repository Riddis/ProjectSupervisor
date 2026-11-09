# The plugin catalog

`index.yaml` here is the framework's plugin catalog: one list, one entry per shared package, in either home
(`docs/PLUGIN_STANDARD.md`, "Where a package lives"). It is data the maintainers list, not a place to write code or
docs about a package - that lives with the package itself, in `packages/<distribution>/` or its own repository.

## Proposing a listing

1. Release the package first (the `plugin-release` skill): version, changelog, the built wheel tested against the
   oldest and newest framework in its range, the exact artifact identified (version, wheel name, sha256, the source
   commit, the tag).
2. Open a pull request to the framework repository that adds or updates exactly one entry in `plugins-catalog/
   index.yaml`, in identity order, with every field `tools/catalog_check.py` checks (version, distribution,
   summary, home, framework range, licence, tag, `artifact.name`/`artifact.sha256`) filled in from that release -
   never from a build the pull request itself triggers. `g5.date` and `g5.framework` are the maintainers' own
   fields (below): leave them for the review, not the proposal, unless the package was already reviewed at this
   exact version and digest.
3. Run `python tools/catalog_check.py` yourself before opening the pull request. A refusal names the entry and the
   field; fix the entry, not the check.

## What the maintainers run before listing it

The pull request's own green run is not taken on trust (CONTRIBUTING.md, "Review: you first, then the
maintainers"). Before merging a catalog pull request, a maintainer:

1. Re-runs `tools/catalog_check.py` on the exact head the pull request proposes.
2. Re-runs **G5 on the exact artifact** named in the entry: the wheel `artifact.name` and `artifact.sha256` name,
   against the framework version the entry's `g5.framework` will record - never a rebuild, and never a different
   artifact that merely claims the same version.
3. Reviews what G5 cannot: that the artifact scans clean of project information (`supervisor boundary words`),
   that its manifest and compatibility evidence hold up, and the trust rule's own question - module-level network
   or subprocess calls, a registry change on import. This human half is the `catalog-review` skill
   (`.claude/skills/catalog-review/SKILL.md`); it is the backstop for judgement the validator cannot carry.
4. Sets `g5.date` to the date of that re-run and `g5.framework` to the framework version it was run against, in the
   same pull request (amending the proposer's entry, or asking them to).

Any one of these missing or failing is a refusal, written by the maintainer on the pull request, naming which half
failed - the validator's (malformed) or the review's (unsafe or unproven). A listing is merged only once both are
satisfied by the same artifact.

## What the entry records

Exactly the artifact the maintainers reviewed: its version, its wheel or sdist name and sha256, the tag that
release lives under, the framework range its own manifest declares, its author's own licence, and the date and
framework version of the maintainers' own G5 run. Nothing else - no download link, no maintainer contact, no
`deprecated` flag (`CONTRIBUTOR_RULING.md` 2.2: the schema version is how fields like that arrive later, not a
free-text column today).

## Updating a version

A later release is a new pull request changing that one entry in place (same identity, new `version`, `tag`,
`artifact`, and - once reviewed - new `g5.date`/`g5.framework`), never a second entry for the same identity: the
validator refuses a duplicate `identity` before a reviewer has to notice it by eye. The superseded version's own
tag and release record stay in the package's history; the index names only the one version currently listed.

## What this does not prove

Read `tools/catalog_check.py`'s own module docstring before trusting "validated" as "safe": it fetches nothing (a
foreign home's URL, tag, artifact and digest are attested by the pull request, not verified by CI), it cannot prove
authorship of a repository it names, and it never imports or runs a package's code. That gap is exactly what the
maintainers' G5 re-run and the `catalog-review` skill exist to close.
