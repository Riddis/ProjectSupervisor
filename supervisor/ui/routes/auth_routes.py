"""Entry points, including redirects for old login bookmarks."""
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/login")
@router.post("/login")
@router.get("/")
def index():
    return RedirectResponse(url="/overview", status_code=303)
