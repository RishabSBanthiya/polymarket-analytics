"""
Performance metrics API endpoints.
"""

from fastapi import APIRouter, Query
from typing import Optional, List

from ..models.schemas import PerformanceMetrics
from ..services.performance_service import PerformanceService

router = APIRouter()
service = PerformanceService()


@router.get("/agents", response_model=List[PerformanceMetrics])
async def get_agents_performance(
    days: int = Query(30, description="Number of days to look back")
):
    """Get performance metrics for all agents"""
    return service.get_all_agents_performance(days=days)


@router.get("/agents/{agent_id}", response_model=PerformanceMetrics)
async def get_agent_performance(
    agent_id: str,
    days: int = Query(30, description="Number of days to look back")
):
    """Get performance metrics for a specific agent"""
    return service.get_agent_performance(agent_id, days=days)


@router.get("/overview")
async def get_overview(
    days: int = Query(30, description="Number of days to look back")
):
    """Get overall performance overview"""
    return service.get_overview(days=days)


@router.get("/timeseries")
async def get_timeseries(
    days: int = Query(30, description="Number of days to look back"),
    interval: str = Query("day", description="Time interval (day/hour)")
):
    """Get performance time series data"""
    return service.get_timeseries(days=days, interval=interval)

