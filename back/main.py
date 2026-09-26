import os
import re
import json
import sqlite3
import uuid
from datetime import datetime
# 【変更】存在しないタスクに 404 を返すため HTTPException を追加
from fastapi import FastAPI, HTTPException
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
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect("tasks.db")
    conn.row_factory = sqlite3.Row
    return conn

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

# ==========================================
# API 1: タスク情報の推測
# ==========================================
@app.post("/api/extract_task")
def extract_task_api(request: TaskExtractRequest):
    now_str = datetime.now().isoformat()
    prompt = render_prompt("extract_task", now_str=now_str, task_description=request.user_input)

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
def save_task_api(request: TaskSaveRequest):
    task_id = str(uuid.uuid4())
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO tasks (id, title, deadline, estimated_minutes, category, status) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, request.title, request.deadline, request.estimated_minutes, request.category, "未着手")
    )
    conn.commit()
    conn.close()
    return {"status": "success", "task_id": task_id}

# ==========================================
# API 3: 全タスクの取得
# ==========================================
@app.get("/api/get_tasks")
def get_tasks_api():
    conn = get_db_connection()
    tasks = conn.execute("SELECT * FROM tasks").fetchall()
    conn.close()
    return [dict(task) for task in tasks]

# ==========================================
# API 4: 今日のスケジュール生成
# ==========================================
@app.post("/api/generate_schedule")
def generate_schedule_api(request: ConditionRequest):
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM tasks WHERE status = '未着手'").fetchall()
    conn.close()
    
    uncompleted_tasks = [dict(row) for row in rows]
    tasks_json = json.dumps(uncompleted_tasks, ensure_ascii=False)
    now_str = datetime.now().isoformat()

    user_profile = ""
    if os.path.exists("user_profile.txt"):
        with open("user_profile.txt", "r", encoding="utf-8") as f:
            user_profile = f.read().strip()

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
def parse_completion_api(request: ParseCompletionRequest):
    conn = get_db_connection()
    rows = conn.execute("SELECT id, title FROM tasks WHERE status = '未着手'").fetchall()
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
def complete_task_api(request: CompleteTaskRequest):
    conn = get_db_connection()
    cursor = conn.execute(
        "UPDATE tasks SET status = '完了', actual_minutes = ? WHERE id = ?",
        (request.actual_minutes, request.task_id)
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
def run_weekly_batch_api():
    conn = get_db_connection()
    completed_tasks = conn.execute("SELECT title, category, estimated_minutes, actual_minutes FROM tasks WHERE status = '完了'").fetchall()
    
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
    
    with open("user_profile.txt", "w", encoding="utf-8") as f:
        f.write(profile_text)
        
    conn.execute("DELETE FROM tasks WHERE status = '完了'")
    conn.commit()
    conn.close()
    
    return {"status": "success", "profile": profile_text}

# ファイルの一番下に追加
# backフォルダの親ディレクトリにある「front」フォルダのパスを取得
base_dir = os.path.dirname(os.path.abspath(__file__))
front_dir = os.path.join(base_dir, "..", "front")

# frontフォルダ内のHTML/CSS/JSを配信
app.mount("/", StaticFiles(directory=front_dir, html=True), name="front")