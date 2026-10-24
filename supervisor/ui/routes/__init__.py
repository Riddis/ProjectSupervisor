from fastapi import APIRouter

from . import agents, auth_routes, chat, inbox, live, log, overview, owner_tasks, roadmap, settings, tasks, thumb

router = APIRouter()
for mod in (auth_routes, overview, roadmap, tasks, agents, inbox, chat, log, owner_tasks, settings, thumb, live):
    router.include_router(mod.router)
