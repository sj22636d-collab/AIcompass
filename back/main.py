from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime
import json
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 開発用。本番時はフロントエンドのURLに制限します
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def load_api_key(filepath="api_key.txt"):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print("api_key.txtが見つかりません。")
        return ""

client = OpenAI(api_key=load_api_key())

class TaskRequest(BaseModel):
    user_input: str

class ConditionRequest(BaseModel):
    condition_text: str

# 疑似データベース（本来はSQLite等を使用します）
DUMMY_DB = []

@app.post("/api/extract_task")
def extract_task_api(request: TaskRequest):
    now_str = datetime.now().isoformat()
    prompt = f"""
    あなたは優秀なタスク管理アシスタントです。
    【現在日時】{now_str}
    ユーザーの入力: {request.user_input}
    
    上記からタスク情報を推測しJSONで出力してください。
    出力形式: {{"title": "タスク名", "deadline": "YYYY-MM-DD", "estimatedMinutes": 数値, "category": "思考系/作業系/コミュニケーション系/インプット系", "reason": "理由"}}
    """
    
    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompt}],
        temperature=0.2,
    )
    result = json.loads(response.choices[0].message.content)
    
    # 疑似DBへ保存（ハッカソン用の簡易処理）
    import uuid
    result["id"] = str(uuid.uuid4())
    DUMMY_DB.append(result)
    
    return result

@app.post("/api/generate_schedule")
def generate_schedule_api(request: ConditionRequest):
    # 疑似DBにタスクがない場合はダミーを入れる（テスト用）
    tasks = DUMMY_DB if len(DUMMY_DB) > 0 else [
        {"id": "T01", "title": "A社への提案書作成", "deadline": "2026-09-25", "estimatedMinutes": 120, "category": "思考系"},
        {"id": "T02", "title": "経費精算の入力", "deadline": "2026-09-30", "estimatedMinutes": 30, "category": "作業系"}
    ]
    tasks_json = json.dumps(tasks, ensure_ascii=False)
    now_str = datetime.now().isoformat()

    prompt = f"""
    あなたはユーザーのコンディションに寄り添うタスク管理アシスタントです。
    【現在日時】{now_str}
    【未完了タスク】{tasks_json}
    【ユーザーの状態】{request.condition_text}
    
    ユーザーの状態に合わせ、今日実行すべきタスクを選定し、順番を組んでJSONで出力してください。
    出力形式: {{"assessed_condition": {{"available_minutes": 数値, "energy_level": "high/medium/low"}}, "schedule": [{{"task_id": "ID", "title": "タスク名", "reason": "理由"}}], "ai_message": "励ましの言葉"}}
    """

    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompt}],
        temperature=0.4,
    )
    return json.loads(response.choices[0].message.content)