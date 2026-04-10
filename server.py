import os
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status, WebSocket, WebSocketDisconnect, Request
from pydantic import BaseModel, EmailStr
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from datetime import datetime
from typing import Optional
import json
from fastapi.middleware.cors import CORSMiddleware
import resend
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import bcrypt

# Logs folder setup
if not os.path.exists("logs"):
    os.makedirs("logs")

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# logger
def setup_logger(name, log_file, level=logging.INFO):
    handler = logging.FileHandler(log_file)
    handler.setFormatter(formatter)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    
    # terminal out
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

# logging files
server_logger = setup_logger('server', 'logs/serverLog.txt')
user_logger = setup_logger('user', 'logs/usersLog.txt')
crash_logger = setup_logger('crash', 'logs/crashLog.txt', level=logging.ERROR)

# Password hash 
def get_password_hash(password: str) -> str:
    pwd_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(pwd_bytes, salt)
    return hashed_password.decode('utf-8') 

def verify_password(plain_password: str, hashed_password: str) -> bool:
    password_bytes = plain_password.encode('utf-8')
    hash_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hash_bytes)

# Pydantic Models
class UserCreate(BaseModel):
    email: EmailStr
    userName: str
    password: str

class UserLogin(BaseModel):
    userName: str
    password: str

class Feedback(BaseModel):
    message: str

# Rate Limiter
limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)

resend.api_key = "re_3ZAwJNuw_GTxG12JoBEHMW342JyjfbTnq"
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/")
client = MongoClient(MONGO_URI)

try:
    client.admin.command('ping')
    server_logger.info("connected to local mongoDB")
    db = client["chat_database"]
    users_collection = db["users"]
except ConnectionFailure:
    crash_logger.error("Failed to connect, might be offline")

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        
        # case insensitive looker
        user = users_collection.find_one({"userName": {"$regex": f"^{username}$", "$options": "i"}})
        real_username = user["userName"] if user else username

        self.active_connections[real_username] = websocket
        server_logger.info(f"[WS] {real_username} CONNECTED. Total active users: {len(self.active_connections)}")
        
        users_collection.update_one({"userName": real_username}, {"$set": {"status": "online"}})

    def disconnect(self, username: str):
        # case insensitive looker - disconnecter
        user = users_collection.find_one({"userName": {"$regex": f"^{username}$", "$options": "i"}})
        real_username = user["userName"] if user else username

        if real_username in self.active_connections:
            del self.active_connections[real_username]
            server_logger.info(f"[WS] {real_username} DISCONNECTED. Total active users: {len(self.active_connections)}")
            
        users_collection.update_one({"userName": real_username}, {"$set": {"status": "offline"}})

    async def broadcast_status(self, username: str, status: str):
        user = users_collection.find_one({"userName": {"$regex": f"^{username}$", "$options": "i"}})

        if user and "friends" in user:
            real_username = user["userName"]
            for friend in user["friends"]:
                if friend in self.active_connections:
                    payload = json.dumps({
                        "type": "status_update",
                        "username": real_username,
                        "status": status
                    })
                    await self.active_connections[friend].send_text(payload)
                    server_logger.info(f"[WS] Pushed '{status}' status of {real_username} to friend: {friend}")

    async def send_personal_message(self, message: dict, receiver_username: str):
        # reciver looker
        user = users_collection.find_one({"userName": {"$regex": f"^{receiver_username}$", "$options": "i"}})
        real_receiver = user["userName"] if user else receiver_username

        if real_receiver in self.active_connections:
            websocket = self.active_connections[real_receiver]
            await websocket.send_text(json.dumps(message))
            server_logger.info(f"[WS] SUCCESS: Routed message from {message.get('from', 'Unknown')} to {real_receiver}")
        else:
            server_logger.warning(f"[WS] FAILED: {real_receiver} is not in active_connections (They are offline!)")


manager = ConnectionManager()


# --- THE WEBSOCKET ENDPOINT ---
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    server_logger.info(f"[WS] Connection attempt from: {username}")
    await manager.connect(websocket, username)
    await manager.broadcast_status(username, "online")
    
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            server_logger.info(f"[WS] Received payload from {username}: {payload}")
            
            if payload["type"] == "chat_message":
                # Fallback for difrent naming
                receiver = payload.get("to") or payload.get("receiver")
                content = payload.get("content")
                sender = payload.get("from") or payload.get("sender") or username
                
                if not receiver or not content:
                    continue

                server_logger.info(f"[WS] Processing chat_message: {sender} -> {receiver}")
                
                db.messages.insert_one({
                    "participants": [sender, receiver],
                    "sender": sender,
                    "content": content,
                    "timestamp": datetime.utcnow()
                })
                server_logger.info("[WS] Message saved to MongoDB.")
                
                await manager.send_personal_message({
                    "type": "chat_message",
                    "from": sender,
                    "content": content
                }, receiver)
                
    except WebSocketDisconnect:
        server_logger.warning(f"[WS] WebSocketDisconnect for {username}")
        manager.disconnect(username)
        await manager.broadcast_status(username, "offline")


@app.get("/ping")
def ping_server():
    return {"status": "ok", "message": "Server is running"}

@app.get("/user-status")
async def get_user_status(userName: str):
    # Case insensitive checker
    user = users_collection.find_one({"userName": {"$regex": f"^{userName}$", "$options": "i"}})
    
    if user and user.get("status") == "online":
        return {"status": "online"}
    
    return {"status": "offline"}

@app.post("/feedback")
@limiter.limit("1/minute") # will wait 1 minute 
async def handle_feedback(request: Request, feedback: Feedback):
    try:
        server_logger.info(f"Feedback received from {request.client.host}")
        params: resend.Emails.SendParams = {
            "from": "Website <noreply@andhyy.com>",
            "to": ["adam.tomala@seznam.cz"],
            "subject": "New Feedback",
            "text": feedback.message
        }
        resend.Emails.send(params)
        server_logger.info(f"Feedback email sent from {request.client.host}")
        return {"status": "success", "message": "Email sent!"}
    except Exception as e:
        crash_logger.error(f"Feedback error: {e}")
        return {"status": "error", "message": "Server error"}

@app.post("/register")
async def register_user(user: UserCreate):
    # another case in checker
    if users_collection.find_one({"$or": [
        {"userName": {"$regex": f"^{user.userName}$", "$options": "i"}}, 
        {"email": {"$regex": f"^{user.email}$", "$options": "i"}}
    ]}):
        raise HTTPException(status_code=400, detail="Username or email already exists")
    
    user_doc = {
        "email": user.email.lower(),
        "userName": user.userName,  # will keep the case sens in DB but lookups are insens
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
    user_logger.info(f"[API] New user registered: {user.userName}")
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

    user_logger.info(f"[API] User logged in: {db_user['userName']}")
    
    return {
        "status": "success", 
        "username": db_user["userName"], # will return case sens in Frontend
        "friends": db_user.get("friends", []),
        "friendRequests": db_user.get("friendRequests", [])
    }

@app.post("/friend-request")
async def friend_request(data: dict):
    # Fallback for diff naming
    sender = data.get("from") or data.get("sender")
    receiver = data.get("to") or data.get("receiver")
    
    if not sender or not receiver:
        return {"status": "error", "message": "Missing sender or receiver details"}

    user_logger.info(f"[API] Friend request: {sender} -> {receiver}")

    # lookup
    sender_user = users_collection.find_one({"userName": {"$regex": f"^{sender}$", "$options": "i"}})
    target_user = users_collection.find_one({"userName": {"$regex": f"^{receiver}$", "$options": "i"}})
    
    if not target_user or not sender_user:
        return {"status": "error", "message": "User not found"}

    real_receiver_name = target_user["userName"]
    real_sender_name = sender_user["userName"]

    if real_sender_name in target_user.get("friends", []):
        return {"status": "error", "message": "Already friends"}
    
    if real_sender_name in target_user.get("friendRequests", []):
        return {"status": "error", "message": "Request already pending"}

    # Update DB 
    users_collection.update_one(
        {"userName": real_receiver_name},
        {"$addToSet": {"friendRequests": real_sender_name}}
    )

    if real_receiver_name in manager.active_connections:
        await manager.active_connections[real_receiver_name].send_text(json.dumps({
            "type": "friend_request",
            "from": real_sender_name
        }))
        user_logger.info(f"[WS] Notified {real_receiver_name} of friend request from {real_sender_name}")

    return {"status": "success", "message": "Request sent"}

@app.post("/respond-friend-request")
async def respond_friend_request(data: dict):
    # cannonProof extractor
    requester = data.get("requester") or data.get("from") or data.get("sender")
    receiver = data.get("receiver") or data.get("to")
    action = data.get("action")     
    
    if not requester or not receiver or not action:
        return {"status": "error", "message": "Missing required data fields"}
        
    user_logger.info(f"[API] Friend request response: {receiver} {action}ed {requester}")

    # Fetch case insens users
    req_user = users_collection.find_one({"userName": {"$regex": f"^{requester}$", "$options": "i"}})
    rec_user = users_collection.find_one({"userName": {"$regex": f"^{receiver}$", "$options": "i"}})

    if not req_user or not rec_user:
        return {"status": "error", "message": "One or both users not found"}

    real_requester = req_user["userName"]
    real_receiver = rec_user["userName"]

    if action == "accept":
        users_collection.update_one(
            {"userName": real_receiver}, 
            {"$pull": {"friendRequests": real_requester}, "$addToSet": {"friends": real_requester}}
        )
        users_collection.update_one(
            {"userName": real_requester}, 
            {"$addToSet": {"friends": real_receiver}}
        )
        
        # Notify 
        if real_requester in manager.active_connections:
            await manager.active_connections[real_requester].send_text(json.dumps({
                "type": "friend_accepted",
                "friend": real_receiver
            }))
            
    elif action == "decline":
        users_collection.update_one(
            {"userName": real_receiver}, 
            {"$pull": {"friendRequests": real_requester}}
        )
        user_logger.info(f"[API] Friend request declined: {real_receiver} declined {real_requester}")
        
    return {"status": "success"}