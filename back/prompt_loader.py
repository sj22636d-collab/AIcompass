import os
from string import Template

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

# === prompts/<name>.txt を読み込み、$変数 を差し込んで返す ===
def render_prompt(name, **kwargs):
    with open(os.path.join(PROMPTS_DIR, f"{name}.txt"), "r", encoding="utf-8") as f:
        return Template(f.read()).substitute(**kwargs)
