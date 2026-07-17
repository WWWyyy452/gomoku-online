"""
五子棋 AI 趣味评语模块
通过大模型 API 生成嘲讽/解说/搞笑评论
支持可配置的模型、性格、API Key
"""

import json
import os
import random
import subprocess

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "ai_config.json")

# 缓存加载的配置
_config = None


def load_config():
    """加载配置，带缓存"""
    global _config
    if _config is not None:
        return _config
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _config = {"enabled": False}
    return _config


def reload_config():
    """强制重新加载配置（热更新用）"""
    global _config
    _config = None
    return load_config()


def board_to_text(board):
    """将棋盘转为可读文本，供大模型理解"""
    symbols = {0: "·", 1: "●", 2: "○"}
    lines = []
    # 列号
    header = "    " + " ".join(f"{c:2d}" for c in range(len(board)))
    lines.append(header)
    for r, row in enumerate(board):
        row_str = " ".join(symbols.get(cell, "?") for cell in row)
        lines.append(f"{r:2d}  {row_str}")
    return "\n".join(lines)


def get_active_personality(config):
    """获取当前设定的性格描述"""
    personalities = config.get("personalities", {})
    active = config.get("active_personality", "随机")

    if active == "随机" or active not in personalities:
        if personalities:
            name, desc = random.choice(list(personalities.items()))
            return name, desc
        return "默认", "你是一个五子棋解说员，用一句话点评这步棋。"

    return active, personalities[active]


def generate_commentary(board, last_move, player, move_count, personality_desc):
    """
    调用大模型生成一句评语
    返回: (personality_name, comment_text) 或 None（失败时）
    """
    config = load_config()
    if not config.get("enabled"):
        return None

    # 优先从环境变量读取，其次从配置文件读取
    api_key = os.environ.get("LONGCAT_API_KEY", "") or config.get("api_key", "")
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        return None

    base_url = config.get("api_base_url", "https://api.longcat.chat/openai/v1").rstrip("/")
    model = config.get("model", "LongCat-Flash-Chat")
    temperature = config.get("temperature", 0.9)
    max_tokens = config.get("max_tokens", 80)

    # 构建局面描述
    board_text = board_to_text(board)
    player_name = "黑棋(●)" if player == 1 else "白棋(○)"
    r, c = last_move

    # 判断局势
    total_stones = sum(1 for row in board for cell in row if cell != 0)
    if total_stones < 10:
        stage = "开局阶段"
    elif total_stones < 40:
        stage = "中盘阶段"
    else:
        stage = "收官阶段"

    user_msg = (
        f"当前棋盘（{stage}，第{move_count}手）：\n{board_text}\n\n"
        f"刚刚 {player_name} 落子于 ({r},{c})。\n"
        f"请用{personality_desc}\n"
        f"要求：只输出一句评语，不要解释，不要加引号，20字以内。"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是五子棋解说员，只用一句话点评。"},
            {"role": "user", "content": user_msg},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # 思考模式配置（false 关闭思考，加快响应）
    if config.get("thinking", False):
        payload["thinking"] = {"type": "enabled"}
    else:
        payload["thinking"] = {"type": "disabled"}

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        # 使用 curl 调用（urllib 对该 API 的 POST 不稳定）
        cmd = [
            "curl", "-s", "-X", "POST",
            f"{base_url}/chat/completions",
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {api_key}",
            "-d", json.dumps(payload, ensure_ascii=False),
            "--max-time", "25",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            print(f"[AI-CHAT] curl 失败: {proc.stderr[:100]}")
            return None
        result = json.loads(proc.stdout)

        message = result.get("choices", [{}])[0].get("message", {})

        # 优先取 content，为空则从 reasoning_content 末尾提取（思考型模型）
        text = message.get("content", "").strip()
        if not text:
            reasoning = message.get("reasoning_content", "").strip()
            if reasoning:
                # 取 reasoning 最后一句非空行作为评语
                lines = [l.strip() for l in reasoning.split("\n") if l.strip()]
                text = lines[-1] if lines else ""

        # 清理多余引号和空白
        text = text.strip('"').strip("'").strip()
        if text and len(text) <= 100:
            return text
        return None

    except (KeyError, IndexError, json.JSONDecodeError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        print(f"[AI-CHAT] 评语生成失败: {e}")
        return None


def get_commentary(board, last_move, player, move_count):
    """
    高层接口：生成一句带性格的评语
    返回: {"personality": "毒舌型", "text": "就这？"} 或 None
    """
    config = load_config()
    if not config.get("enabled"):
        return None

    personality_name, personality_desc = get_active_personality(config)
    text = generate_commentary(board, last_move, player, move_count, personality_desc)

    if text:
        return {"personality": personality_name, "text": text}
    return None
