"""FastAPI 静态路由；目录内容在启动索引结束后发布，而不是导入模块时生成。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


class GalleryFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers['Cache-Control'] = 'no-cache'
        return response


def register_route(driver, public_path: Path) -> bool:
    app = getattr(driver, 'server_app', None)
    if not isinstance(app, FastAPI):
        return False
    if not any(getattr(route, 'path', None) == '/randpic' for route in app.routes):
        app.mount('/randpic', GalleryFiles(directory=str(public_path), html=True, check_dir=False), name='randpic')
    return True
