from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import getpass
import uvicorn

app = FastAPI()

# middleware to allow CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)

# incoming requst
class LoginRequest(BaseModel):
    name: str = "Anonymous"

# POST endpoint
@app.post("/login")
def login(request_data: LoginRequest):
    print(f"Ping received from client user: {request_data.name}")
    # username for Hello
    server_user = getpass.getuser() 
    return {
        "status": "success",
        "server_user": server_user
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)