---
name: catalog-review
description: Review a pull request that lists or updates a package in the framework's plugin catalog (plugins-catalog/index.yaml) - the maintainers' own G5 re-run on the exact artifact, the manifest and compatibility evidence, and what the entry then records. Use when asked to review, approve or list a catalog entry, "catalog review", "/catalog-review", or when a pull request touches plugins-catalog/index.yaml.
---

# Catalog review: listing a package

`tools/catalog_check.py` already refused the malformed (CONTRIBUTOR_RULING.md section 2.4: a missing field, an
unsatisfiable range, a duplicate identity, a home naming neither shape). This skill is the other half: whether the
one exact artifact the entry names is *safe* to list, which no deterministic check can decide. Run both: a clean
`catalog_check.py` is necessary and never sufficient.

## 1. The artifact, identified by version and digest

Read the pull request's entry, not its description:
- `identity`, `distribution`, `version`, `home`, `tag`.
- `artifact.name` and `artifact.sha256` - the one wheel or sdist this review is about. Fetch (or, for a
  `packages/` home, build) exactly that file and compare its sha256 yourself:
  `python -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" <file>`.
  A mismatch is a refusal before anything else runs - the entry is not describing the file in front of you.
- For a `packages/` home, the tag names a commit in this repository: check it out (or `git archive`) and build the
  wheel from that commit, never from a working tree that might have moved on.

## 2. Manifest and compatibility evidence verified

- `supervisor plugin check <identity>` (G5) against the installed artifact, in a fresh environment with no source
  on the path - the plugin-release skill's section 2, step 4, repeated here rather than taken on trust.
- The manifest's `framework` range matches the entry's `framework`, and the entry's `g5.framework` is a version
  inside that range.
- The package's own suite, against the installed wheel, on at least the framework version named in `g5.framework`.
- No project's information in the artifact: `supervisor boundary words <package folder or wheel>`, run where the
  local project-terms file is (plugin-release's own closing check, re-run here rather than read from the pull
  request).
- The trust rule's own question (`docs/plan/boundary/00-main.md`, "Other suggestions"): module-level network or
  subprocess calls in the package's own code, and any registry change on a clean-process import. G5's
  `import_side_effects` and `registrations` checks catch the second; the first is a reading of the source, because
  a side effect inside a *dependency's* import escapes both - which is exactly why this review exists beside them.

## 3. G5 re-run by the maintainer, on that exact artifact

Not the contributor's own run, and not a rebuild: the same file whose digest section 1 verified, run by the
maintainer listing it. Record the date and the framework version it was run against - these become the entry's
own `g5.date` and `g5.framework` (CONTRIBUTOR_RULING.md 2.2: "the date of the maintainers' own G5 run", "one
version, the one the maintainers ran"). A G5 run against a different framework version than the one about to be
recorded is not this check; run it again against that version.

## 4. What the entry then records, exactly

Once sections 1-3 pass, the merged entry carries:
- the artifact's own version, wheel/sdist name and sha256, and the tag it was built from;
- the framework range its manifest declares, and the licence it declares (recorded, never approved - plugins keep
  their authors' own licence);
- `g5.date` and `g5.framework` set to *this* review's own run, not the proposer's.

Nothing else: no maintainer contact, no download link, no `deprecated` flag. A later release is the same entry
updated in place (plugins-catalog/README.md, "Updating a version"), not a second one.

## 5. Refusal

Any of these missing or failing is a refusal, written on the pull request, naming which half failed:
- **The validator's half** (mechanical, already run): `tools/catalog_check.py` is not clean on the exact head.
- **This skill's half** (judgement): the digest does not match the file reviewed; G5 fails, on the artifact or on
  the maintainer's own re-run; the suite fails; a project word is found; the trust rule's question finds a
  module-level network, subprocess or import-time registry side effect with no stated reason; the licence the
  entry names is not the one the package declares.

A refusal is written evidence (which check, what it found), the same as a boundary-review REVISE - never a silent
close. T4-14 is this skill's own proof: one candidate exercised to **two** separate refusals, the validator's exit
1 and this skill's own written one, because they answer different questions.

## Which gates apply

A catalog review changes no framework code - it reads a pull request and runs checks against an already-built
artifact. So, per `docs/plan/boundary/00-main.md`'s "Which gates apply" table: **G5 (re-run by the maintainer on
the listed artifact) and G7 (this skill's own questions, answered in writing) apply; G1-G4 do not - there is no
framework code change for them to gate.** `tools/catalog_check.py` itself is covered by G3 (the framework's own
suite, `tests/test_catalog.py`) when the validator's own code changes, not by this skill reviewing a listing.

## Checks it ends with

- The artifact's digest verified against the file actually reviewed (section 1).
- G5 passed on that artifact, re-run by the maintainer, against the framework version about to be recorded.
- The package's own suite passed on the installed wheel.
- No project information in the artifact or its folder.
- The entry's `g5.date`/`g5.framework` set to this review's run, and every other field matching the artifact
  reviewed - or a written refusal naming which check failed.
