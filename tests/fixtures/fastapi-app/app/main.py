from fastapi import FastAPI

app = FastAPI(title="r2t fastapi fixture")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"status": "r2t fastapi fixture is running"}
