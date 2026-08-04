"""Aggregate local routes for job-source adapters."""

from fastapi import APIRouter

from .ats.router import router as ats_router
from .nowcoder.router import router as nowcoder_router


router = APIRouter()
router.include_router(ats_router)
router.include_router(nowcoder_router)
