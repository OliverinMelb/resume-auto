"""Aggregate local routes for job-source adapters."""

from fastapi import APIRouter

from .eleduck.router import router as eleduck_router
from .nowcoder.router import router as nowcoder_router


router = APIRouter()
router.include_router(nowcoder_router)
router.include_router(eleduck_router)
