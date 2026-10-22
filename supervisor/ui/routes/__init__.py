from fastapi import APIRouter

from . import agents, auth_routes, chat, inbox, log, overview, owner_tasks, roadmap, settings, tasks, thumb

router = APIRouter()
for mod in (auth_routes, overview, roadmap, tasks, agents, inbox, chat, log, owner_tasks, settings, thumb):
    router.include_router(mod.router)
