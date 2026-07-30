"""Aggregate local routes for job-source adapters."""

from fastapi import APIRouter

from .nowcoder.router import router as nowcoder_router


router = APIRouter()
router.include_router(nowcoder_router)
