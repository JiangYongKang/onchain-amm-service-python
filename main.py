from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from amm_service.api import router
from amm_service.errors import AmmError

app = FastAPI(title="Local AMM Service")
app.include_router(router)


@app.exception_handler(AmmError)
async def amm_error_handler(_: Request, exc: AmmError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": {"code": exc.code, "message": str(exc)}},
    )


@app.get("/")
def read_root() -> dict:
    return {"Hello": "World"}
