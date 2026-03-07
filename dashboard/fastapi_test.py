from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient

app = FastAPI()
r = APIRouter()

@r.get("/")
def test_slash():
    return {"msg": "slash"}

app.include_router(r, prefix="/test")

@app.get("/{path:path}")
def fallback(path: str):
    return {"fallback": path}

client = TestClient(app)
print("GET /test: ", client.get("/test").json())
print("GET /test/: ", client.get("/test/").json())
