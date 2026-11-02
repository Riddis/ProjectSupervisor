"""The configuration this plugin owns: `plugins.review_rounds` in a project's config/routing.yaml, validated by
this model before setup() runs. Read it with `supervisor.plugin.config(layout, "review_rounds")`.

Every value a project-local extension of this shape used to hardcode - which score fields an inbox card may show
(`score_keys`), which of a round's own row fields the grid tiles display as scores (`tile_score_fields`), which
extra filter controls the grid offers (`filters`) and where a labelling session's finished label file lands
(`labels_path_template`) - is a key here, with a neutral default: no score field is assumed, no extra filter is
shown, and the label file lands under the project's own `state/` unless a project names somewhere else."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Filter(BaseModel):
    """One extra filter control the grid renders, alongside the verdict filter every round already has. `field`
    names a key the round's own rows carry (read directly off each row, independent of `tile_score_fields` -
    a project may filter on a field it never shows); `kind` says how the control compares it: `select`
    (`options` lists the choices), `number_min` (the field must be at least the entered value) or
    `text_contains` (a case-insensitive substring match)."""
    field: str
    label: str
    kind: Literal["select", "number_min", "text_contains"] = "text_contains"
    options: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    score_keys: list[str] = Field(default_factory=list)
    tile_score_fields: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    labels_path_template: str = "state/qa/labels/{item_id}.yaml"
