import os
import json
from datetime import datetime
from openai import OpenAI

# --- 追加: 外部のテキストファイルからAPIキーを読み込む関数 ---
def load_api_key(filepath="api_key.txt"):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            # strip()で前後の改行や空白を取り除く
            return f.read().strip()
    except FileNotFoundError:
        print(f"エラー: {filepath} が見つかりません。ファイルを作成してAPIキーを記述してください。")
        exit(1)

# ファイルからキーを取得してクライアントを初期化
api_key = load_api_key("api_key.txt")
client = OpenAI(api_key=api_key)

def extract_task_info(user_input: str, current_date: str) -> dict:
    prompt = f"""
    あなたは優秀なタスク管理アシスタントです。
    ユーザーの入力した自然言語のタスクメモから、以下の情報を推測・抽出してJSON形式で出力してください。

    【基準となる現在の日時】
    {current_date}

    【タスクの性質の分類】
    以下の4つから最も適切なものを1つ選んでください。
    - 思考系 (集中力や論理的思考が必要：資料作成、設計など)
    - 作業系 (手や体を動かす単純作業：データ入力、掃除など)
    - コミュニケーション系 (他者とのやり取り：メール返信、電話、チャットなど)
    - インプット系 (情報の吸収：読書、調査、動画視聴など)

    【出力形式（厳密なJSON）】
    {{
      "title": "タスク名（簡潔に要約したもの）",
      "deadline": "期限 (YYYY-MM-DD形式。不明な場合は null)",
      "estimatedMinutes": 見積もり所要時間（分単位の数値。例: 1.5時間なら 90）,
      "category": "分類したタスクの性質",
      "reason": "なぜその見積もり時間と性質に分類したかの短い理由"
    }}
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_input}
            ],
            temperature=0.2,
        )
        
        task_data = json.loads(response.choices[0].message.content)
        return task_data

    except Exception as e:
        print(f"API呼び出しエラー: {e}")
        raise e

if __name__ == "__main__":
    user_text = "来週火曜までに研究の進捗出して資料作る"
    now_str = datetime.now().isoformat()
    
    result = extract_task_info(user_text, now_str)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    #sasasasas
    