"""Vendored primitives: the artifact/manifest contract (`contract`), the handoff protocol (`handoff`) and the usage
window policy (`usage_policy`). They predate the framework (first written for an earlier agent supervisor, then
carried into the project this framework was extracted from) and are kept as a self-contained package: nothing here
imports from the rest of `supervisor`, so they can be lifted again unchanged.
"""
