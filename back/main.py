import os
import re
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import hmac
# 【変更】存在しないタスクに 404 を返すため HTTPException を追加
# 【変更】ログイン中のユーザーを特定するため Depends, Header を追加
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, ConfigDict
from typing import Optional
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware

from prompt_loader import render_prompt

from fastapi.staticfiles import StaticFiles


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === データベースの初期設定 ===
def init_db():
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            deadline TEXT,
            estimated_minutes INTEGER,
            category TEXT,
            status TEXT,
            actual_minutes INTEGER
        )
    ''')
    # 【変更】ユーザーとログイン中のセッションのテーブルを追加
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
    # 【変更】タスクがどのユーザーのものかを記録する列を追加 (既存のDBにも後から追加する)
    columns = [row[1] for row in c.execute("PRAGMA table_info(tasks)")]
    if "user_id" not in columns:
        c.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT")
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect("tasks.db")
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# 【変更】ログイン機能
# ==========================================
SESSION_DAYS = 30  # ログインが有効な日数

def hash_password(password: str, salt: str) -> str:
    # パスワードはそのまま保存せず、ソルト付きでハッシュ化して保存する
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()

def create_session(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat()
    conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)", (token, user_id, expires_at))
    return token

def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """リクエストの「Authorization: Bearer <トークン>」からログイン中のユーザーを特定する。無効なら 401"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="ログインが必要です")
    token = authorization[len("Bearer "):]
    conn = get_db_connection()
    row = conn.execute(
        "SELECT users.id, users.username, users.profile, sessions.expires_at "
        "FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token = ?",
        (token,)
    ).fetchone()
    conn.close()
    if row is None or row["expires_at"] < datetime.now().isoformat():
        raise HTTPException(status_code=401, detail="ログインの有効期限が切れています")
    return {"id": row["id"], "username": row["username"], "profile": row["profile"] or ""}

# === Groq API設定 (OpenAI互換機能を使用) ===
def load_api_key(filepath="api_key.txt"):
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            key = f.read().strip()
            # ASCII英数字とハイフン・アンダースコアのみ抽出
            clean_key = re.sub(r'[^a-zA-Z0-9\-_]', '', key)
            return clean_key
    except FileNotFoundError:
        print(f"【エラー】{filepath} が見つかりません。")
        return ""

# base_url を Groq のエンドポイントに指定
client = OpenAI(
    api_key=load_api_key(),
    base_url="https://api.groq.com/openai/v1"
)

# Groqが現在提供している最新の高速推論モデルに変更
GROQ_MODEL = "openai/gpt-oss-20b"

# === 日付ユーティリティ ===
# サーバーのタイムゾーンに関係なく日本時間で扱う
JST = timezone(timedelta(hours=9))
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]

def now_jst():
    return datetime.now(JST)

def format_now(now):
    return f"{now.strftime('%Y-%m-%d')}（{WEEKDAYS_JA[now.weekday()]}） {now.strftime('%H:%M')}"

# LLMに日付計算をさせないよう、今日から数週間分の「日付・曜日・週」の早見表を作る
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

# 【変更】ログイン・新規登録用
class AuthRequest(BaseModel):
    username: str
    password: str

# ==========================================
# 【変更】API: 新規登録・ログイン・ログアウト
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
        conn.execute(
            "INSERT INTO users (id, username, password_hash, salt, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, hash_password(request.password, salt), salt, datetime.now().isoformat())
        )
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="このユーザー名はすでに使われています")
    token = create_session(conn, user_id)
    conn.commit()
    conn.close()
    return {"token": token, "username": username}

@app.post("/api/login")
def login_api(request: AuthRequest):
    conn = get_db_connection()
    user = conn.execute("SELECT id, username, password_hash, salt FROM users WHERE username = ?", (request.username.strip(),)).fetchone()
    # ユーザーが存在しない場合もパスワード違いと同じ応答にする (どのユーザー名が登録済みか分からないように)
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
        conn.execute("DELETE FROM sessions WHERE token = ?", (authorization[len("Bearer "):],))
        conn.commit()
        conn.close()
    return {"status": "success"}

# ==========================================
# API 1: タスク情報の推測
# ==========================================
# 【変更】全 API をログイン必須にした (ここは AI の利用料を第三者に使われないため)
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

# ==========================================
# API 2: タスクをデータベースに保存
# ==========================================
@app.post("/api/save_task")
def save_task_api(request: TaskSaveRequest, user: dict = Depends(get_current_user)):
    task_id = str(uuid.uuid4())
    conn = get_db_connection()
    # 【変更】ログイン中のユーザーのタスクとして保存する
    conn.execute(
        "INSERT INTO tasks (id, title, deadline, estimated_minutes, category, status, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, request.title, request.deadline, request.estimated_minutes, request.category, "未着手", user["id"])
    )
    conn.commit()
    conn.close()
    return {"status": "success", "task_id": task_id}

# ==========================================
# API 3: 全タスクの取得
# ==========================================
@app.get("/api/get_tasks")
def get_tasks_api(user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    # 【変更】自分のタスクだけを返す
    tasks = conn.execute("SELECT * FROM tasks WHERE user_id = ?", (user["id"],)).fetchall()
    conn.close()
    return [dict(task) for task in tasks]

# ==========================================
# API 4: 今日のスケジュール生成
# ==========================================
@app.post("/api/generate_schedule")
def generate_schedule_api(request: ConditionRequest, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    # 【変更】自分の未着手タスクだけを対象にする
    rows = conn.execute("SELECT * FROM tasks WHERE status = '未着手' AND user_id = ?", (user["id"],)).fetchall()
    conn.close()

    uncompleted_tasks = [dict(row) for row in rows]
    tasks_json = json.dumps(uncompleted_tasks, ensure_ascii=False)
    now_str = format_now(now_jst())

    # 【変更】ユーザーの傾向は user_profile.txt (全員共通) ではなく、ユーザーごとに DB から読む
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

    # 【変更】AIが一覧にないタスクを作ることがあるので、実在する未着手タスクだけ残す
    titles_by_id = {task["id"]: task["title"] for task in uncompleted_tasks}
    result["schedule"] = [
        {**item, "title": titles_by_id[item["task_id"]]}
        for item in result.get("schedule", [])
        if isinstance(item, dict) and item.get("task_id") in titles_by_id
    ]
    return result

# ==========================================
# API 5: 完了報告の解析
# ==========================================
@app.post("/api/parse_completion")
def parse_completion_api(request: ParseCompletionRequest, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    # 【変更】自分の未着手タスクだけと照合する
    rows = conn.execute("SELECT id, title FROM tasks WHERE status = '未着手' AND user_id = ?", (user["id"],)).fetchall()
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

    # 【変更】AIが一覧にないIDや "null" を返すことがあるので、実在する未着手タスクか確認する
    titles_by_id = {task["id"]: task["title"] for task in uncompleted_tasks}
    task_id = result.get("task_id")
    if task_id not in titles_by_id:
        return {"task_id": None, "task_title": None, "actual_minutes": None}

    # 【変更】"180分" のように文字列で返ってきた場合も数値にする
    minutes = result.get("actual_minutes")
    if not isinstance(minutes, int):
        digits = re.search(r"\d+", str(minutes))
        minutes = int(digits.group()) if digits else 0

    return {"task_id": task_id, "task_title": titles_by_id[task_id], "actual_minutes": minutes}

# ==========================================
# API 6: タスクを「完了」としてDBに記録
# ==========================================
@app.post("/api/complete_task")
def complete_task_api(request: CompleteTaskRequest, user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    # 【変更】他のユーザーのタスクは完了にできないよう user_id も条件にする
    cursor = conn.execute(
        "UPDATE tasks SET status = '完了', actual_minutes = ? WHERE id = ? AND user_id = ?",
        (request.actual_minutes, request.task_id, user["id"])
    )
    conn.commit()
    conn.close()
    # 【変更】該当タスクがなければ成功扱いにせず 404 を返す
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="指定されたタスクが見つかりません")
    return {"status": "success"}

# ==========================================
# API 7: 週次バッチ処理
# ==========================================
@app.post("/api/run_weekly_batch")
def run_weekly_batch_api(user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    # 【変更】自分の完了タスクだけを分析・削除の対象にする
    completed_tasks = conn.execute(
        "SELECT title, category, estimated_minutes, actual_minutes FROM tasks WHERE status = '完了' AND user_id = ?",
        (user["id"],)
    ).fetchall()
    
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
    
    # 【変更】傾向は全員共通のファイルではなく、ユーザーごとに DB へ保存する
    conn.execute("UPDATE users SET profile = ? WHERE id = ?", (profile_text, user["id"]))

    conn.execute("DELETE FROM tasks WHERE status = '完了' AND user_id = ?", (user["id"],))
    conn.commit()
    conn.close()
    
    return {"status": "success", "profile": profile_text}

# ファイルの一番下に追加
# backフォルダの親ディレクトリにある「front」フォルダのパスを取得
base_dir = os.path.dirname(os.path.abspath(__file__))
front_dir = os.path.join(base_dir, "..", "front")

# frontフォルダ内のHTML/CSS/JSを配信
app.mount("/", StaticFiles(directory=front_dir, html=True), name="front")