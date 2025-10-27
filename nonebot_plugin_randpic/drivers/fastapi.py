from pathlib import Path

from fastapi.staticfiles import StaticFiles

from nonebot.drivers.fastapi import Driver


def register_route(driver: Driver, randpic_puclic_path: Path):
    app = driver.server_app

    static_path = str((randpic_puclic_path).resolve())

    app.mount("/randpic", StaticFiles(directory=static_path, html=True), name="randpic")