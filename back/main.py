import os
import re
import json
import uuid
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import hmac

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, ConfigDict
from typing import Optional
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware
from .prompt_loader import render_prompt
from fastapi.staticfiles import StaticFiles

# 【変更】SQLite の代わりに psycopg2 をインポート
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 【変更】データベース接続設定 (Neon / PostgreSQL)
# ==========================================
# ↓ 先ほどNeonでコピーした Connection string に書き換えてください！ ↓
# 1. データベースURLの読み込みを変更
# Render上では "DATABASE_URL" を使い、見つからない場合のみ直接書いたURLを使う
NEON_DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_bqKI0TRa2kFd@ep-billowing-wildflower-b3x8mxx3-pooler.c-4.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")

def get_db_connection():
    # 結果を辞書型(dict)で受け取れるように RealDictCursor を使用
    conn = psycopg2.connect(NEON_DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db_connection()
    # psycopg2では直接 conn.execute が使えないため、cursor を使います
    with conn.cursor() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                deadline TEXT,
                estimated_minutes INTEGER,
                category TEXT,
                status TEXT,
                actual_minutes INTEGER,
                user_id TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                profile TEXT DEFAULT '',
                created_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        ''')
        # PostgreSQLの構文でカラム追加 (すでに存在する場合はスキップされる)
        c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS user_id TEXT")
    conn.commit()
    conn.close()

init_db()

# ==========================================
# ログイン機能
# ==========================================
SESSION_DAYS = 30

def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()

def create_session(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat()
    with conn.cursor() as cur:
        # SQLiteの ? の代わりに %s を使います
        cur.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, %s)", (token, user_id, expires_at))
    return token

def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="ログインが必要です")
    token = authorization[len("Bearer "):]
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT users.id, users.username, users.profile, sessions.expires_at "
            "FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token = %s",
            (token,)
        )
        row = cur.fetchone()
    conn.close()
    if row is None or row["expires_at"] < datetime.now().isoformat():
        raise HTTPException(status_code=401, detail="ログインの有効期限が切れています")
    return {"id": row["id"], "username": row["username"], "profile": row["profile"] or ""}

# 2. APIキーの読み込み関数を変更
def load_api_key(filepath="api_key.txt"):
    # Render上では環境変数から読み込む
    if "GROQ_API_KEY" in os.environ:
        return os.environ["GROQ_API_KEY"]
        
    # ローカル環境ではファイルから読み込む
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            key = f.read().strip()
            return re.sub(r'[^a-zA-Z0-9\-_]', '', key)
    except FileNotFoundError:
        return ""

client = OpenAI(
    api_key=load_api_key(),
    base_url="https://api.groq.com/openai/v1"
)
GROQ_MODEL = "openai/gpt-oss-20b"

# === 日付ユーティリティ ===
JST = timezone(timedelta(hours=9))
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]

def now_jst():
    return datetime.now(JST)

def format_now(now):
    return f"{now.strftime('%Y-%m-%d')}（{WEEKDAYS_JA[now.weekday()]}） {now.strftime('%H:%M')}"

def build_date_table(now, days=35):
    today = now.date()
    this_monday = today - timedelta(days=today.weekday())
    week_labels = ["今週", "来週", "再来週"]
    special = {0: "今日", 1: "明日", 2: "明後日"}
    lines = []
    for i in range(days):
        d = today + timedelta(days=i)
        week_index = (d - this_monday).days // 7
        week = week_labels[week_index] if week_index < len(week_labels) else f"{week_index}週間後"
        label = f"{week}の{WEEKDAYS_JA[d.weekday()]}曜"
        if i in special:
            label = f"{special[i]}、{label}"
        lines.append(f"{d.isoformat()}（{WEEKDAYS_JA[d.weekday()]}） {label}")
    return "\n".join(lines)

# === リクエストスキーマ ===
class TaskExtractRequest(BaseModel):
    user_input: str

class TaskSaveRequest(BaseModel):
    title: str
    deadline: Optional[str] = ""
    estimated_minutes: Optional[int] = 0
    category: Optional[str] = ""

class ConditionRequest(BaseModel):
    condition_text: str

class ParseCompletionRequest(BaseModel):
    report_text: str

class CompleteTaskRequest(BaseModel):
    task_id: str
    actual_minutes: int

class AuthRequest(BaseModel):
    username: str
    password: str

# ==========================================
# API: 新規登録・ログイン・ログアウト
# ==========================================
@app.post("/api/register")
def register_api(request: AuthRequest):
    username = request.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        raise HTTPException(status_code=400, detail="ユーザー名は半角英数字と _ で3〜20文字にしてください")
    if len(request.password) < 8:
        raise HTTPException(status_code=400, detail="パスワードは8文字以上にしてください")

    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(16)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, username, password_hash, salt, created_at) VALUES (%s, %s, %s, %s, %s)",
                (user_id, username, hash_password(request.password, salt), salt, datetime.now().isoformat())
            )
        token = create_session(conn, user_id)
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback() # エラー時はロールバックが必要
        conn.close()
        raise HTTPException(status_code=409, detail="このユーザー名はすでに使われています")
    
    conn.close()
    return {"token": token, "username": username}

@app.post("/api/login")
def login_api(request: AuthRequest):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, password_hash, salt FROM users WHERE username = %s", (request.username.strip(),))
        user = cur.fetchone()
        
    if user is None or not hmac.compare_digest(user["password_hash"], hash_password(request.password, user["salt"])):
        conn.close()
        raise HTTPException(status_code=401, detail="ユーザー名かパスワードが違います")
    token = create_session(conn, user["id"])
    conn.commit()
    conn.close()
    return {"token": token, "username": user["username"]}

@app.post("/api/logout")
def logout_api(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token = %s", (authorization[len("Bearer "):],))
        conn.commit()
        conn.close()
    return {"status": "success"}

# ==========================================
# 各種タスクAPI
# ==========================================
@app.post("/api/extract_task")
def extract_task_api(request: TaskExtractRequest):
    now = now_jst()
    prompt = render_prompt(
        "extract_task",
        now_str=format_now(now),
        date_table=build_date_table(now),
        task_description=request.user_input,
    )
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompt}],
        temperature=0.2,
    )
    return json.loads(response.choices[0].message.content)

@app.post("/api/save_task")
def save_task_api(request: TaskSaveRequest, user: dict = Depends(get_current_user)):
    task_id = str(uuid.uuid4())
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tasks (id, title, deadline, estimated_minutes, category, status, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (task_id, request.title, request.deadline, request.estimated_minutes, request.category, "未着手", user["id"])
        )
    conn.commit()
    conn.close()
    return {"status": "success", "task_id": task_id}

@app.get("/api/get_tasks")
def get_tasks_api(user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM tasks WHERE user_id = %s", (user["id"],))
        tasks = cur.fetchall()
    conn.close()
    return [dict(task) for task in tasks]

@app.post("/api/generate_schedule")
def generate_schedule_api(request: ConditionRequest, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM tasks WHERE status = '未着手' AND user_id = %s", (user["id"],))
        rows = cur.fetchall()
    conn.close()

    uncompleted_tasks = [dict(row) for row in rows]
    tasks_json = json.dumps(uncompleted_tasks, ensure_ascii=False)
    now_str = format_now(now_jst())
    user_profile = user["profile"]

    prompt = render_prompt(
        "generate_schedule",
        now_str=now_str,
        user_profile=user_profile,
        pending_tasks_json=tasks_json,
        user_condition=request.condition_text,
    )

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompt}],
        temperature=0.4,
    )
    result = json.loads(response.choices[0].message.content)

    titles_by_id = {task["id"]: task["title"] for task in uncompleted_tasks}
    result["schedule"] = [
        {**item, "title": titles_by_id[item["task_id"]]}
        for item in result.get("schedule", [])
        if isinstance(item, dict) and item.get("task_id") in titles_by_id
    ]
    return result

@app.post("/api/parse_completion")
def parse_completion_api(request: ParseCompletionRequest, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, title FROM tasks WHERE status = '未着手' AND user_id = %s", (user["id"],))
        rows = cur.fetchall()
    conn.close()
    
    uncompleted_tasks = [dict(row) for row in rows]
    tasks_json = json.dumps(uncompleted_tasks, ensure_ascii=False)

    prompt = render_prompt("parse_completion", pending_tasks_json=tasks_json, completion_report=request.report_text)
    
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompt}],
        temperature=0.1,
    )
    result = json.loads(response.choices[0].message.content)

    titles_by_id = {task["id"]: task["title"] for task in uncompleted_tasks}
    task_id = result.get("task_id")
    if task_id not in titles_by_id:
        return {"task_id": None, "task_title": None, "actual_minutes": None}

    minutes = result.get("actual_minutes")
    if not isinstance(minutes, int):
        digits = re.search(r"\d+", str(minutes))
        minutes = int(digits.group()) if digits else 0

    return {"task_id": task_id, "task_title": titles_by_id[task_id], "actual_minutes": minutes}

@app.post("/api/complete_task")
def complete_task_api(request: CompleteTaskRequest, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status = '完了', actual_minutes = %s WHERE id = %s AND user_id = %s",
            (request.actual_minutes, request.task_id, user["id"])
        )
        rowcount = cur.rowcount
    conn.commit()
    conn.close()
    
    if rowcount == 0:
        raise HTTPException(status_code=404, detail="指定されたタスクが見つかりません")
    return {"status": "success"}

@app.post("/api/run_weekly_batch")
def run_weekly_batch_api(user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, category, estimated_minutes, actual_minutes FROM tasks WHERE status = '完了' AND user_id = %s",
            (user["id"],)
        )
        completed_tasks = cur.fetchall()
        
        if not completed_tasks:
            conn.close()
            return {"status": "no_data", "message": "分析する完了タスクがありません。"}
            
        tasks_data = [dict(row) for row in completed_tasks]
        tasks_json = json.dumps(tasks_data, ensure_ascii=False)
        
        prompt = render_prompt("weekly_profile", completed_tasks_json=tasks_json)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        profile_text = response.choices[0].message.content
        
        cur.execute("UPDATE users SET profile = %s WHERE id = %s", (profile_text, user["id"]))
        cur.execute("DELETE FROM tasks WHERE status = '完了' AND user_id = %s", (user["id"],))
        
    conn.commit()
    conn.close()
    return {"status": "success", "profile": profile_text}

base_dir = os.path.dirname(os.path.abspath(__file__))
front_dir = os.path.join(base_dir, "..", "front")
app.mount("/", StaticFiles(directory=front_dir, html=True), name="front")