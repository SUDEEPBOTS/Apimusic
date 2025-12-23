import os
import uuid
import subprocess
import requests
import threading
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from dotenv import load_dotenv
import google.generativeai as genai

# ─── LOAD ENV ─────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")          # STORAGE BOT TOKEN
UPLOAD_CHAT_ID = os.getenv("UPLOAD_CHAT_ID")
MONGO_URL = os.getenv("MONGO_URL")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# ─── GEMINI ───────────────────────────────
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# ─── DB ───────────────────────────────────
client = MongoClient(MONGO_URL)
db = client.music_api
songs = db.songs

# ─── APP ──────────────────────────────────
app = FastAPI()
os.makedirs("tmp", exist_ok=True)

# ─── CORS ─────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── HEALTH ───────────────────────────────
@app.api_route("/", methods=["GET", "HEAD", "OPTIONS"])
def health(response: Response):
    return {"status": "alive"}

# ─── GEMINI MATCH ─────────────────────────
def gemini_match(text: str) -> str:
    prompt = f"""
Convert this into best YouTube music search query.
Only return the query text.
Input: {text}
"""
    res = model.generate_content(prompt)
    return res.text.strip()

# ─── YT-DLP DOWNLOAD ──────────────────────
def download_song(query: str) -> str:
    filename = f"tmp/{uuid.uuid4().hex}.mp3"
    subprocess.run(
        [
            "yt-dlp",
            "-x", "--audio-format", "mp3",
            "--cookies", "cookies.txt",
            "-o", filename,
            f"ytsearch1:{query}"
        ],
        check=True
    )
    return filename

# ─── TELEGRAM UPLOAD (STORAGE BOT) ─────────
def upload_to_telegram(path: str) -> str:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
    with open(path, "rb") as f:
        r = requests.post(
            url,
            data={"chat_id": UPLOAD_CHAT_ID},
            files={"audio": f}
        )
    return r.json()["result"]["audio"]["file_id"]

# ─── FILE URL GENERATOR (IMPORTANT FIX) ────
def get_file_url(file_id: str) -> str:
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id}
    )
    file_path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

# ─── BACKGROUND JOB ───────────────────────
def process_song(user_query: str, final_query: str):
    try:
        path = download_song(final_query)
        file_id = upload_to_telegram(path)
        file_url = get_file_url(file_id)

        songs.update_one(
            {"user_query": user_query},
            {
                "$set": {
                    "user_query": user_query,
                    "final_query": final_query,
                    "file_url": file_url,
                    "status": "ready"
                }
            },
            upsert=True
        )
    except Exception as e:
        songs.update_one(
            {"user_query": user_query},
            {
                "$set": {
                    "status": "error",
                    "error": str(e)
                }
            },
            upsert=True
        )

# ─── MAIN API ─────────────────────────────
@app.post("/music")
def music_api(data: dict):
    raw_query = data.get("query")
    if not raw_query:
        return {"error": "query required"}

    # NORMALIZE
    user_query = raw_query.strip().lower()

    song = songs.find_one({"user_query": user_query})

    if song:
        if song.get("status") == "ready":
            return {
                "status": "cached",
                "file_url": song["file_url"]
            }
        elif song.get("status") == "processing":
            return {"status": "processing"}
        elif song.get("status") == "error":
            return {"status": "error"}

    final_query = gemini_match(user_query)

    songs.update_one(
        {"user_query": user_query},
        {"$set": {"status": "processing", "final_query": final_query}},
        upsert=True
    )

    threading.Thread(
        target=process_song,
        args=(user_query, final_query),
        daemon=True
    ).start()

    return {"status": "processing"}