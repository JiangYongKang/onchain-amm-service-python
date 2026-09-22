"""服务入口：uvicorn main:app 启动本地做市资金池服务。"""

from app.api import create_app

app = create_app()
