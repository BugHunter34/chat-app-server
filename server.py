import os
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, EmailStr
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from passlib.context import CryptContext
from datetime import datetime
from typing import Optional
from fastapi import WebSocket, WebSocketDisconnect
import json
from fastapi.middleware.cors import CORSMiddleware


# Logs folder setup
if not os.path.exists("logs"):
    os.makedirs("logs")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Password Hashing Setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

# Pydantic Models
class UserCreate(BaseModel):
    email: EmailStr
    userName: str
    password: str

class UserLogin(BaseModel):
    userName: str
    password: str

# API and MongoDB setup
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)

# load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://Admin:<pass>@chatdb.llkjj5f.mongodb.net/") 
client = MongoClient(MONGO_URI)

try:
    client.admin.command('ping')
    logger.info("Successfully connected to MongoDB!")
    db = client["chat_database"]
    users_collection = db["users"]
except ConnectionFailure:
    logger.error("Failed to connect to MongoDB.")

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections[username] = websocket
        logger.info(f"[WS] {username} CONNECTED. Total active users: {len(self.active_connections)}")
        
        # Update DB
        users_collection.update_one({"userName": username}, {"$set": {"status": "online"}})
        
        # Notify friends
        await self.broadcast_status(username, "online")

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]
            logger.info(f"[WS] {username} DISCONNECTED. Total active users: {len(self.active_connections)}")
            
        # Update DB
        users_collection.update_one({"userName": username}, {"$set": {"status": "offline"}})
        
        # Notify friends - TODO

    async def send_personal_message(self, message: dict, receiver_username: str):
        if receiver_username in self.active_connections:
            websocket = self.active_connections[receiver_username]
            await websocket.send_text(json.dumps(message))
            logger.info(f"[WS] SUCCESS: Routed message from {message['from']} to {receiver_username}")
        else:
            logger.warning(f"[WS] FAILED: {receiver_username} is not in active_connections (They are offline!)")

    async def broadcast_status(self, username: str, status: str):
        user = users_collection.find_one({"userName": username})
        if user and "friends" in user:
            for friend in user["friends"]:
                if friend in self.active_connections:
                    await self.active_connections[friend].send_text(json.dumps({
                        "type": "status_update",
                        "username": username,
                        "status": status
                    }))

manager = ConnectionManager()

# --- THE WEBSOCKET ENDPOINT ---
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    logger.info(f"[WS] Connection attempt from: {username}")
    await manager.connect(websocket, username)
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            
            logger.info(f"[WS] Received payload from {username}: {payload}")
            
            if payload["type"] == "chat_message":
                receiver = payload["to"]
                content = payload["content"]
                
                logger.info(f"[WS] Processing chat_message: {username} -> {receiver}")
                
                # Save to MongoDB
                db.messages.insert_one({
                    "participants": [username, receiver],
                    "sender": username,
                    "content": content,
                    "timestamp": datetime.utcnow()
                })
                logger.info("[WS] Message saved to MongoDB.")
                
                # Route message to receiver
                await manager.send_personal_message({
                    "type": "chat_message",
                    "from": username,
                    "content": content
                }, receiver)
                
            elif payload["type"] == "friend_request":
                pass

    except WebSocketDisconnect:
        logger.warning(f"[WS] WebSocketDisconnect exception fired for {username}")
        manager.disconnect(username)


@app.get("/ping")
def ping_server():
    return {"status": "ok", "message": "Server is running"}

@app.post("/register")
async def register_user(user: UserCreate):
    if users_collection.find_one({"$or": [{"userName": user.userName}, {"email": user.email}]}):
        raise HTTPException(status_code=400, detail="Username or email already exists")
    
    user_doc = {
        "email": user.email.lower(),
        "userName": user.userName,
        "passwordHash": get_password_hash(user.password),
        "role": "user",
        "isBanned": False,
        "isVerified": False,
        "createdAt": datetime.utcnow(),
        "friends": [],
        "friendRequests": [],
        "status": "offline"
    }
    
    users_collection.insert_one(user_doc)
    logger.info(f"[API] New user registered: {user.userName}")
    return {"status": "success", "message": "User created!"}

@app.post("/login")
async def login_user(user: UserLogin):
    db_user = users_collection.find_one({"userName": {"$regex": f"^{user.userName}$", "$options": "i"}})
    if not db_user:
        return {"status": "error", "message": "Invalid username or password"}
    
    stored_hash = db_user.get("passwordHash")
    if not stored_hash or not verify_password(user.password, stored_hash):
        return {"status": "error", "message": "Invalid username or password"}
        
    if db_user.get("isBanned"):
        return {"status": "error", "message": "This account is banned."}

    logger.info(f"[API] User logged in: {db_user['userName']}")
    
    return {
        "status": "success", 
        "username": db_user["userName"],
        "friends": db_user.get("friends", []),
        "friendRequests": db_user.get("friendRequests", [])
    }

@app.post("/friend-request")
async def friend_request(data: dict):
    sender = data.get("from")
    receiver = data.get("to")
    logger.info(f"[API] Friend request: {sender} -> {receiver}")

    target_user = users_collection.find_one({"userName": {"$regex": f"^{receiver}$", "$options": "i"}})
    if not target_user:
        return {"status": "error", "message": "User not found"}

    real_receiver_name = target_user["userName"]

    if sender in target_user.get("friends", []):
        return {"status": "error", "message": "Already friends"}
    
    if sender in target_user.get("friendRequests", []):
        return {"status": "error", "message": "Request already pending"}

    users_collection.update_one(
        {"userName": real_receiver_name},
        {"$addToSet": {"friendRequests": sender}}
    )

    if real_receiver_name in manager.active_connections:
        await manager.active_connections[real_receiver_name].send_text(json.dumps({
            "type": "friend_request",
            "from": sender
        }))
        logger.info(f"[WS] Notified {real_receiver_name} of friend request from {sender}")

    return {"status": "success", "message": "Request sent"}

@app.post("/respond-friend-request")
async def respond_friend_request(data: dict):
    requester = data.get("requester")
    receiver = data.get("receiver") 
    action = data.get("action")     
    logger.info(f"[API] Friend request response: {receiver} {action}ed {requester}")

    if action == "accept":
        users_collection.update_one(
            {"userName": receiver}, 
            {"$pull": {"friendRequests": requester}, "$addToSet": {"friends": requester}}
        )
        users_collection.update_one(
            {"userName": requester}, 
            {"$addToSet": {"friends": receiver}}
        )
        
        if requester in manager.active_connections:
            await manager.active_connections[requester].send_text(json.dumps({
                "type": "friend_accepted",
                "friend": receiver
            }))
            
    elif action == "decline":
        users_collection.update_one(
            {"userName": receiver}, 
            {"$pull": {"friendRequests": requester}}
        )

    return {"status": "success"}