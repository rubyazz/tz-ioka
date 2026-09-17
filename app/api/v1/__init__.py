"""API v1 routers. Mounted under ``settings.api_prefix`` (= /travel)."""

from fastapi import APIRouter

from app.api.v1 import auth, locations, offers, orders

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(locations.router)
api_router.include_router(offers.router)
api_router.include_router(orders.router)
