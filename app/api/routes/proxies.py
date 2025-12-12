"""
代理列表 API 路由
Proxy list API routes for managing dynamic proxy pool.
"""

from pydantic import BaseModel
from fastapi import APIRouter

from app.core.config import settings
from app.services.proxy import proxy_service

router = APIRouter()


class ProxiesRead(BaseModel):
    """Response model for proxy list read."""

    content: str
    count: int


class ProxiesUpdate(BaseModel):
    """Request model for proxy list update."""

    content: str


@router.get("", response_model=ProxiesRead)
async def get_proxies() -> ProxiesRead:
    """
    获取代理列表文件内容和解析数量
    Get proxy list file content and parsed count.
    """
    proxies_file = settings.data_folder / "proxies.txt"

    content = ""
    if proxies_file.exists():
        content = proxies_file.read_text(encoding="utf-8")

    return ProxiesRead(
        content=content,
        count=proxy_service.proxy_count,
    )


@router.put("", response_model=ProxiesRead)
async def update_proxies(update: ProxiesUpdate) -> ProxiesRead:
    """
    更新代理列表文件并重新加载
    Update proxy list file and reload.
    """
    proxies_file = settings.data_folder / "proxies.txt"

    # Ensure data folder exists
    settings.data_folder.mkdir(parents=True, exist_ok=True)

    # Write content to file
    proxies_file.write_text(update.content, encoding="utf-8")

    # Reload proxies
    count = await proxy_service.load_from_content(update.content)

    return ProxiesRead(
        content=update.content,
        count=count,
    )


@router.get("/status")
async def get_proxy_status():
    """
    获取代理池状态
    Get proxy pool status for monitoring.
    """
    return proxy_service.get_status()
