from fastapi import APIRouter

from metis_web.views import auth, chat, settings, workspace

router = APIRouter()
for module in (auth, workspace, chat, settings):
    router.include_router(module.router)
