import datetime
from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import getpass
import uvicorn
import os
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# --- config ---
app = FastAPI()
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://Admin:<secret>@chatdb.llkjj5f.mongodb.net/") 
client = MongoClient(MONGO_URI)

# --- MongoDB Connection ---
try:
    client.admin.command('ping')
    print("Successfully connected to MongoDB!")
    db = client["chat_database"]
    users_collection = db["users"]
except ConnectionFailure:
    print("Failed to connect to MongoDB.")
    

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
    # MongoDB insert
    users_collection.insert_one({"name": request_data.name, "timestamp": datetime.utcnow()})

    return {
        "status": "success",
        "server_user": server_user
    }
    
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)