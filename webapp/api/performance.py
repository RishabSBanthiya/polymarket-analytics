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
    days: int = Query(30, description="Number of days to look back"),
    include_orphans: bool = Query(True, description="Include orphan trades in performance")
):
    """Get performance metrics for all agents including orphan agent"""
    return await service.get_all_agents_performance_async(
        days=days,
        include_orphans=include_orphans
    )


@router.get("/agents/{agent_id}", response_model=PerformanceMetrics)
async def get_agent_performance(
    agent_id: str,
    days: int = Query(30, description="Number of days to look back"),
    include_orphans: bool = Query(True, description="Include orphan trades for orphan agent")
):
    """Get performance metrics for a specific agent"""
    return await service.get_agent_performance_async(
        agent_id, 
        days=days,
        include_orphans=include_orphans
    )


@router.get("/overview")
async def get_overview(
    days: Optional[int] = Query(None, description="Number of days to look back (None for all-time)"),
    include_orphans: bool = Query(True, description="Include orphan trades in overview")
):
    """Get overall performance overview including orphan trades"""
    # If days is None or 0, use a very large number to get all-time data
    if days is None or days <= 0:
        days = 36500  # ~100 years, effectively all-time
    return await service.get_overview_async(
        days=days,
        include_orphans=include_orphans
    )


@router.get("/timeseries")
async def get_timeseries(
    days: int = Query(30, description="Number of days to look back"),
    interval: str = Query("day", description="Time interval (day/hour)"),
    include_orphans: bool = Query(True, description="Include orphan trades in timeseries")
):
    """Get performance time series data including orphan trades"""
    return await service.get_timeseries_async(
        days=days, 
        interval=interval,
        include_orphans=include_orphans
    )

