"""Control Center: FastAPI + Jinja/HTMX UI over the state store. The UI implements no policy — every
mutating route submits the same command kinds the CLI does (supervisor.commands); it never mutates state/ directly."""
