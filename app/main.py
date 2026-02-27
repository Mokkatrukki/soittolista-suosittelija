"""FastAPI-sovellus — pääpiste."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import os
from dotenv import load_dotenv

load_dotenv()

from app.auth import router as auth_router
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Soittolista-suosittelija", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "dev-secret-vaihda-tuotannossa"),
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)

templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user_id = request.session.get("user_id")
    display_name = request.session.get("display_name")
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "user_id": user_id, "display_name": display_name},
    )
