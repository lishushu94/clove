from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.core.config import settings


# 注册静态文件路由
def register_static_routes(app: FastAPI):
    """Register static file routes for the application."""
    index_path = settings.static_folder / "index.html"
    assets_path = settings.static_folder / "assets"

    # 情况 1：静态目录不存在 → 开发模式
    if not settings.static_folder.exists():
        logger.info(
            "Static folder not found, skipping static routes (development mode)"
        )
        return

    # 情况 2：目录存在但构建产物不完整 → 警告
    if not index_path.exists() or not assets_path.exists():
        missing = []
        if not index_path.exists():
            missing.append("index.html")
        if not assets_path.exists():
            missing.append("assets/")
        logger.warning(
            f"Static folder exists but missing: {', '.join(missing)}. "
            "Run 'pnpm build' in the front directory to build the frontend."
        )
        return

    # 情况 3：构建产物完整 → 注册静态路由
    app.mount("/assets", StaticFiles(directory=str(assets_path)), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve index.html for all non-API routes (SPA support)."""
        return FileResponse(str(index_path))

    logger.info("Frontend static files registered")
