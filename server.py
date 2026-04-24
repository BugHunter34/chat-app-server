import os
import logging
import re
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, status, WebSocket, WebSocketDisconnect, Request
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from datetime import datetime, timedelta
from typing import Optional
import json
from fastapi.middleware.cors import CORSMiddleware
import resend
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import bcrypt
import jwt
from fastapi.staticfiles import StaticFiles
from fastapi import UploadFile, File
import uuid
import random
import secrets
import string
import filetype 

load_dotenv()

# logger
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
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


# JWT secret generator --fallback if the Token isn't in env
# every Token goes invalid
def generate_secret_key():
    # combine letters
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()_+-=[]|;:,.<>?"
    # more secretly generate the key
    secret = ''.join(secrets.choice(alphabet) for _ in range(50))
    return secret

# values
ALGORITHM = "HS256"
SECRET_KEY = os.getenv("SECRET_KEY", generate_secret_key())
resend.api_key = os.getenv("RESEND_API_KEY", "re_ERROR")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/")
client = MongoClient(MONGO_URI)


# try MongoDB
try:
    client.admin.command('ping')
    server_logger.info("connected to local mongoDB")
    db = client["chat_database"]
    users_collection = db["users"]
except ConnectionFailure:
    crash_logger.error("Failed to connect, might be offline")

       
# Logs folder setup
for folder in ["logs", "emojis", "images", "sounds", "avatars", "download"]:
    if not os.path.exists(folder):
        os.makedirs(folder)


# Web token gen for Session
def create_access_token(data: dict):
    to_encode = data.copy()
    # valid for 24h
    expire = datetime.utcnow() + timedelta(hours=24)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# current user from token
# code that extracts user from token, found on StackOverflow, modified and understood
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
async def get_current_user(token: str = Depends(oauth2_scheme)):
    """verify JWT and grab user"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return payload  # return username and role
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired or invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )


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
    allow_origins=["https://andhyy.com","https://chat.andhyy.com", "http://localhost:3000", "https://www.andhyy.com"], 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)
# static files folder paths
app.mount("/emojis", StaticFiles(directory="emojis"), name="emojis")
app.mount("/images", StaticFiles(directory="images"), name="images")
app.mount("/sounds", StaticFiles(directory="sounds"), name="sounds")
app.mount("/avatars", StaticFiles(directory="avatars"), name="avatars")
app.mount("/download", StaticFiles(directory="download"), name="download")


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()

        # regex inject prevention
        safe_username = re.escape(username)

        # case insensitive looker
        user = users_collection.find_one({"userName": {"$regex": f"^{safe_username}$", "$options": "i"}})
        real_username = user["userName"] if user else username

        self.active_connections[real_username] = websocket
        server_logger.info(f"[WS] {real_username} CONNECTED. Total active users: {len(self.active_connections)}")
        
        users_collection.update_one({"userName": real_username}, {"$set": {"status": "online"}})

    def disconnect(self, username: str):

        # regex inject prevention
        safe_username = re.escape(username)

        # case insensitive looker - disconnecter
        user = users_collection.find_one({"userName": {"$regex": f"^{safe_username}$", "$options": "i"}})
        real_username = user["userName"] if user else username

        if real_username in self.active_connections:
            del self.active_connections[real_username]
            server_logger.info(f"[WS] {real_username} DISCONNECTED. Total active users: {len(self.active_connections)}")
            
        users_collection.update_one({"userName": real_username}, {"$set": {"status": "offline"}})

    async def broadcast_status(self, username: str, status: str):

        # regex inject prevention
        safe_username = re.escape(username)

        user = users_collection.find_one({"userName": {"$regex": f"^{safe_username}$", "$options": "i"}})

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
        # regex inject prevention
        safe_receiver_username = re.escape(receiver_username)

        # reciver looker
        user = users_collection.find_one({"userName": {"$regex": f"^{safe_receiver_username}$", "$options": "i"}})
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
async def websocket_endpoint(websocket: WebSocket, username: str, token: str = Query(...)):
    server_logger.info(f"[WS] Connection attempt from: {username}")
    
    # validate token 
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        token_username = payload.get("sub")
        
        if not token_username or token_username.lower() != username.lower():
            server_logger.warning(f"[SECURITY] Invalid WS impersonation attempt for {username}")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    # ----------------
    # safe to connect
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
                sender = username

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

# api.andhyy.com routes
@app.get("/")
@app.get("/ping")
@app.get("/hello")
def ping_server():
    return {"status": "ok", "message": "Server is running"}


@app.get("/user-status")
async def get_user_status(userName: str):
    # regex inject prevention
    safe_username = re.escape(userName)
    # Case insensitive checker
    user = users_collection.find_one({"userName": {"$regex": f"^{safe_username}$", "$options": "i"}})
    avatar_url = user.get("avatarUrl", "https://api.andhyy.com/avatars/default-avatar.gif")
    if not user:
        return {"status": "offline", "avatarUrl": "https://api.andhyy.com/avatars/default-avatar.png"}
    
    if user and user.get("status") == "online":
        return {"status": "online", "avatarUrl": avatar_url}
    
    return {"status": "offline", "avatarUrl": avatar_url}

# show all users to Admin (only basic info)
@app.get("/all-users")
async def get_all_users(current_user: dict = Depends(get_current_user)):
    # check JWT role
    if current_user.get("role") != "admin":
         raise HTTPException(status_code=403, detail="Unauthorized. Admins only.")
    
    users = users_collection.find({}, {"userName": 1, "email": 1, "role": 1, "status": 1, "_id": 0})
    return {"status": "success", "users": list(users)}


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
    
@app.post("/upload")
async def upload_image(request: Request, sender: str = None, receiver: str = None):
    try:
        content_type = request.headers.get("content-type", "")
        
        # file data extraction
        if "multipart/form-data" in content_type:
            form = await request.form()
            # find first file (multipart could contain other fields)
            file_obj = next((v for v in form.values() if hasattr(v, "filename")), None)
            if not file_obj:
                return {"status": "error", "message": "No file found"}
            
            file_bytes = await file_obj.read()
            # ask for extension in rawbytes via filetype guesser
            guess_ext = filetype.guess(file_bytes)
            if guess_ext:
                ext = guess_ext.extension
            # if it fails we'll use the original in the name (could be malicious if somone sents .png.exe)
            else:
                ext = file_obj.filename.split(".")[-1].lower() if "." in file_obj.filename else "bin"
                
        else:
            # fallback if user is on old client Version that used older func without multipart 
            file_bytes = await request.body()
            
            # rawbytes guesser 
            guess_ext = filetype.guess(file_bytes)
            if guess_ext:
                ext = guess_ext.extension
            else:
                ext = "bin" # fallback if it fails, prevents malicious extensions

        # -----------  
        # Mem protect (under 50MB per file)
        if not file_bytes:
            return {"status": "error", "message": "Empty file"}
        if len(file_bytes) > 50 * 1024 * 1024:
            return {"status": "error", "message": "File > 50MB, send smaller one!"}
            
        # routing for WEB
        if sender and receiver:
            # Saving random name to avoid same names
            filename = f"{uuid.uuid4()}.{ext}"
            filepath = os.path.join("images", filename)
            
            with open(filepath, "wb") as f:
                f.write(file_bytes)
                
            server_logger.info(f"File uploaded: {filename} ({len(file_bytes)} bytes)")
            final_url = f"https://api.andhyy.com/images/{filename}"
            msg_text = f"[FILE]{final_url}"
            
            # MongoDB
            db.messages.insert_one({
                "participants": [sender, receiver],
                "sender": sender,
                "content": msg_text,
                "timestamp": datetime.utcnow()
            })
            
            # Notify receiver
            await manager.send_personal_message(
                {"type": "chat_message", "from": sender, "content": msg_text}, receiver
            )
            
            server_logger.info(f"[API] Server routed file for WEB client: {sender} -> {receiver}")
        
        # If there's only sender it means he uploaded avatar 
        elif sender and not receiver:
            # regex inject prevention
            safe_sender = re.escape(sender)

            # Finds User in DB
            user_doc = users_collection.find_one({"userName": {"$regex": f"^{safe_sender}$", "$options": "i"}})
            if not user_doc:
                return {"status": "error", "message": "User not found"}
                
            # Current avatar version
            current_version = user_doc.get("avatarVersion", 0)
            new_version = current_version + 1
            
            # Create specific naming like "john4.png" (fourth avatar of John)
            filename = f"{user_doc['userName']}{new_version}.{ext}"
            filepath = os.path.join("avatars", filename) # saves to /avatars/
            
            with open(filepath, "wb") as f:
                f.write(file_bytes)
                
            final_url = f"https://api.andhyy.com/avatars/{filename}"
            
            # Update DB with new avatar and version
            users_collection.update_one(
                {"_id": user_doc["_id"]},
                {"$set": {"avatarUrl": final_url, "avatarVersion": new_version}}
            )
            
            user_logger.info(f"[API] {user_doc['userName']} changed their avatar to {filename}")
        
        else:
            return {"status": "error", "message": "missing parameters for routing"}

        return {"status": "success", "url": final_url}
        
    except Exception as e:
        crash_logger.error(f"[API] File upload error: {e}")
        return {"status": "error", "message": "Server error"}
    
    
FORBIDDEN_USERNAMES = {"admin", "root", "system", "moderator", "support", "*", "null", "undefined", "operator", "sysadmin", "administrator", "owner", "webmaster", "NONE", "ALL", "ANY", "GUEST", "ANONYMOUS", "TEST", "USER", "USERNAME"}
@app.post("/register")
async def register_user(user: UserCreate):

    # check Forbidden names
    if user.userName.lower() in FORBIDDEN_USERNAMES:
        user_logger.info(f"[SECURITY] Attempt to register with forbidden username: {user.userName}")
        raise HTTPException(status_code=400, detail="Forbidden: username is not allowed")
    

    # prevent regex injection
    safe_username = re.escape(user.userName)
    safe_email = re.escape(user.email)

    # check DB safely 
    if users_collection.find_one({"$or": [
        {"userName": {"$regex": f"^{safe_username}$", "$options": "i"}}, 
        {"email": {"$regex": f"^{safe_email}$", "$options": "i"}}
    ]}):
        raise HTTPException(status_code=400, detail="Username or email already exists")
    
    user_doc = {
        "email": user.email.lower(),
        "userName": user.userName,  # keep case sensitive names for Displaying
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
    # prevent regex injection
    safe_username = re.escape(user.userName)

    # safe query
    db_user = users_collection.find_one({"userName": {"$regex": f"^{safe_username}$", "$options": "i"}})
    if not db_user:
        return {"status": "error", "message": "Invalid username or password"}
    
    stored_hash = db_user.get("passwordHash")
    
    if not stored_hash or not verify_password(user.password, stored_hash):
        return {"status": "error", "message": "Invalid username or password"}
        
    if db_user.get("isBanned"):
        return {"status": "error", "message": "This account is banned."}

    user_logger.info(f"[API] User logged in: {db_user['userName']}")
    # Generate 24h JWT token
    access_token = create_access_token(data={"sub": db_user["userName"], "role": db_user.get("role", "user")})
    
    return {
        "status": "success", 
        "username": db_user["userName"], # will return case sens in Frontend
        "role": db_user.get("role", "user"),
        "friends": db_user.get("friends", []),
        "friendRequests": db_user.get("friendRequests", []),
        "token": access_token
    }

# Get Avatar URL of user
@app.get("/current-avatar")
async def get_current_avatar(current_user: dict = Depends(get_current_user)):
    requester = current_user.get("sub")
    user_doc = users_collection.find_one({"userName": {"$regex": f"^{requester}$", "$options": "i"}})
    if not user_doc:
        return {"status": "error", "message": "User not found"}
    return {"status": "success", "avatarUrl": user_doc.get("avatarUrl")}

@app.post("/profile")
async def update_profile(data: dict, current_user: dict = Depends(get_current_user)):
    requester = current_user.get("sub")
    new_password = data.get("password")
    new_avatar = data.get("avatarUrl")
    new_username = data.get("userName")

    update_fields = {}

    #regex inject prevention
    safe_requester = re.escape(requester)
    safe_new_username = re.escape(new_username)
    
    if new_username and new_username.lower() != requester.lower():

        # will check availability of username
        existing_user = users_collection.find_one({"userName": {"$regex": f"^{safe_new_username}$", "$options": "i"}})
        if existing_user:
            return {"status": "error", "message": "Username is already taken!"}
        update_fields["userName"] = new_username
    
    # Update DB
    users_collection.update_one(
            {"userName": {"$regex": f"^{safe_requester}$", "$options": "i"}},
            {"$push": {"usernameHistory": requester}} # Push moves old username into history list
        )
    
    if new_password:
        update_fields["passwordHash"] = get_password_hash(new_password)
    
    if new_avatar:
        update_fields["avatarUrl"] = new_avatar

    if not update_fields:
        return {"status": "error", "message": "got no changes to update"}
    
    users_collection.update_one({"userName": {"$regex": f"^{safe_requester}$", "$options": "i"}}, {"$set": update_fields})

    # --- Username change handler ---
    if "userName" in update_fields:
        real_new_name = update_fields["userName"]
        
        # 1) Update all messages to new sender username
        db.messages.update_many({"sender": requester}, {"$set": {"sender": real_new_name}})
        # 2) Update participants in messages
        db.messages.update_many({"participants": requester}, {"$set": {"participants.$": real_new_name}})
        # 3) Update friend lists of ALL users where new username must be changed
        users_collection.update_many({"friends": requester}, {"$set": {"friends.$": real_new_name}})
        # 4) Update friend requests of ALL users where new username must be changed
        users_collection.update_many({"friendRequests": requester}, {"$set": {"friendRequests.$": real_new_name}})
        
        user_logger.info(f"[API] User {requester} changed name to {real_new_name}")
        
        # --- JWT Token issue (must be regenerated) ---
        new_token = create_access_token(
            data={"sub": real_new_name, "role": current_user.get("role")}    
        )
        
        return {
            "status": "success", 
            "message": "Profile updated!",
            "new_token": new_token,      # new token
            "new_username": real_new_name # new name
        }

    user_logger.info(f"[API] User updated profile: {requester}")
    return {"status": "success", "message": "updated profile"}

@app.get("/verify-token")
async def verify_token(token: str):
    """auto login via Token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")

        # regex inject prevention
        safe_username = re.escape(username)

        # Fetch data for UI
        db_user = users_collection.find_one({"userName": {"$regex": f"^{safe_username}$", "$options": "i"}})
        if not db_user:
            return {"status": "error"}
            
        return {
            "status": "success",
            "username": db_user["userName"],
            "role": db_user.get("role", "user"),
            "friends": db_user.get("friends", []),
            "friendRequests": db_user.get("friendRequests", [])
        }
    except Exception:
        return {"status": "error", "message": "Token expired or invalid"}

# --- Messages history---
@app.get("/messages")
async def get_messages(user1: str, user2: str, current_user: dict = Depends(get_current_user)):
    requester = current_user.get("sub")
    
    # ensure that requester is authorized (either user1 or user2) not MIM
    if requester.lower() not in [user1.lower(), user2.lower()]:
        raise HTTPException(status_code=403, detail="You can only view your own messages, bad boy")

    # query messages between users
    messages = db.messages.find({
        "participants": {"$all": [user1, user2]}
    }).sort("timestamp", 1).limit(999999) 
    
    history = [{"sender": msg["sender"], "content": msg["content"]} for msg in messages]
    return {"status": "success", "messages": history}

# Admin only route to promote users to admin
@app.post("/promote")
async def promote_user(data: dict, current_user: dict = Depends(get_current_user)):
    # JWT grab
    requester = current_user.get("sub") 
    target_user = data.get("target")
    # regex inject prevention
    safe_requester = re.escape(requester)
    safe_target = re.escape(target_user) if target_user else None

    # admin check
    if current_user.get("role") != "admin":
        user_logger.warning(f"[SECURITY] {requester} tried to OP {target_user} without permission! badGuy")
        raise HTTPException(status_code=403, detail="Unauthorized. not an admin.")
    
    req_db_user = users_collection.find_one({"userName": {"$regex": f"^{safe_requester}$", "$options": "i"}})
    
    if not req_db_user or req_db_user.get("role") != "admin":
        user_logger.warning(f"[SECURITY] {requester} tryed to OP {target_user} without permission! badGuy")
        return {"status": "error", "message": "Unauthorized. not an admin."}

    # find target
    target_db_user = users_collection.find_one({"userName": {"$regex": f"^{safe_target}$", "$options": "i"}})
    if not target_db_user:
        return {"status": "error", "message": "Target not found"}

    real_target_name = target_db_user["userName"]

    # already admin
    if target_db_user.get("role") == "admin":
        return {"status": "error", "message": f"{real_target_name} is already an admin"}

    users_collection.update_one(
        {"userName": real_target_name},
        {"$set": {"role": "admin"}}
    )
    
    user_logger.info(f"[API] {requester} promoted {real_target_name} to admin.")
    return {"status": "success", "message": f"{real_target_name} is now an Admin!"}


@app.post("/friend-request")
async def friend_request(data: dict, current_user: dict = Depends(get_current_user)):
    # sender from JWT
    sender = current_user.get("sub")
    receiver = data.get("to") or data.get("receiver")

    # prevent regex injection
    safe_sender = re.escape(sender)
    safe_target = re.escape(receiver) if receiver else None

    if not receiver:
        return {"status": "error", "message": "Missing receiver details"}
    user_logger.info(f"[API] Friend request: {sender} -> {receiver}")

    # lookup
    sender_user = users_collection.find_one({"userName": {"$regex": f"^{safe_sender}$", "$options": "i"}})
    target_user = users_collection.find_one({"userName": {"$regex": f"^{safe_target}$", "$options": "i"}})
    
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
    # secure extractor
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