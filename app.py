import os
import re
import uuid
import random
import asyncio
import time
import json
from typing import List, Dict
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import edge_tts

# ================= 基础与并发配置 =================
TTS_DIR = "static/mp3_tts"
TEMPLATES_DIR = "templates"
TASKS_JSON_PATH = "tasks.json"  # 任务硬盘持久化文件
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "my_admin_secret_123")  # 清理路由 Token

os.makedirs(TTS_DIR, exist_ok=True)

PART_CHAR_LIMIT = 10000  # 10,000 字自动分卷
CHUNK_TARGET_SIZE = 400  # 400 字换人

# 🔥 最多允许 2 个任务同时向微软发起网络合成，超出自动排队，防止被封 IP
MAX_CONCURRENT_TASKS = 2  
TTS_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# 默认角色列表及基准参数
VOICES_CONFIG = {
    "zh-CN-XiaoxiaoNeural":         {"name": "晓晓 (标准女声)", "weight": 40, "volume": "+0%",   "base_rate": 0},
    "zh-CN-YunyangNeural":          {"name": "云扬 (标准男声)", "weight": 50, "volume": "+50%",  "base_rate": -15},
    "zh-TW-HsiaoChenNeural":        {"name": "HsiaoChen (台湾女声)", "weight": 30, "volume": "+0%",   "base_rate": 0},
    "zh-TW-HsiaoYuNeural":          {"name": "HsiaoYu (台湾女声)", "weight": 10, "volume": "+30%",  "base_rate": 20},
    "zh-CN-liaoning-XiaobeiNeural": {"name": "小贝 (东北口音)", "weight": 10, "volume": "+45%",  "base_rate": 10}
}

REPLACEMENTS = [
    ("#", " "),
    ("DW中文有Instagram！欢迎搜寻dw.chinese，看更多深入浅出的图文与影音报道。", ""),
    (r"© \d{4}年德国之声版权声明：本文所有内容受到著作权法保护，如无德国之声特别授权，不得擅自使用。任何不当行为都将导致追偿，并受到刑事追究。", ""),
    ("To view this video please enable JavaScript, and consider upgrading to a web browser that supports HTML5 video", ""),
    ("加入公視會員，按讚收藏你關注的報導", ""),
    ("（德新社）", ""),
    ("\r\n", "\n")
]

TASKS_DB: Dict[str, dict] = {}

# ================= 持久化与僵尸任务自动修复 =================

def load_tasks_from_disk():
    global TASKS_DB
    if os.path.exists(TASKS_JSON_PATH):
        try:
            with open(TASKS_JSON_PATH, "r", encoding="utf-8") as f:
                TASKS_DB = json.load(f)

            # 自动修复僵尸任务：重启前中断的任务标记为 error
            need_save = False
            for task_id, task in TASKS_DB.items():
                if task.get("status") in ["processing", "queued"]:
                    task["status"] = "error"
                    task["error_message"] = "服务器曾意外重启，后台合成中断，请重新点击提交！"
                    need_save = True

            if need_save:
                save_tasks_to_disk()

        except Exception as e:
            print(f"读取 tasks.json 失败: {e}")
            TASKS_DB = {}

def save_tasks_to_disk():
    try:
        with open(TASKS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(TASKS_DB, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"持久化失败: {e}")

load_tasks_from_disk()

# ================= 文本与语音工具函数 =================

def clean_text(text: str) -> str:
    for old, new in REPLACEMENTS:
        if old.startswith(r"©"):
            text = re.sub(old, new, text)
        else:
            text = text.replace(old, new)
    return text.strip()

def split_into_parts(text: str, max_part_size: int = PART_CHAR_LIMIT) -> List[str]:
    if len(text) <= max_part_size:
        return [text]

    paragraphs = re.split(r'(\n\s*\n|(?<=[。！？]))', text)
    parts, current_part = [], ""

    for p in paragraphs:
        if len(current_part) + len(p) > max_part_size and current_part:
            parts.append(current_part.strip())
            current_part = p
        else:
            current_part += p

    if current_part.strip():
        parts.append(current_part.strip())

    return parts

def split_part_into_chunks(part_text: str, target_size: int = CHUNK_TARGET_SIZE) -> List[str]:
    lines = [line.strip() for line in part_text.split('\n') if line.strip()]
    chunks, current_chunk = [], ""

    for line in lines:
        if len(current_chunk) + len(line) >= target_size and current_chunk:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = (current_chunk + "\n" + line) if current_chunk else line

    if current_chunk:
        chunks.append(current_chunk)

    final_chunks = []
    for c in chunks:
        if len(c) > target_size * 1.5:
            sentences = re.split(r'(?<=[。！？!?])', c)
            sub_chunk = ""
            for s in sentences:
                if len(sub_chunk) + len(s) >= target_size and sub_chunk:
                    final_chunks.append(sub_chunk)
                    sub_chunk = s
                else:
                    sub_chunk += s
            if sub_chunk:
                final_chunks.append(sub_chunk)
        else:
            final_chunks.append(c)

    return [fc.strip() for fc in final_chunks if fc.strip()]

def get_next_voice(allowed_voices: List[str], last_voice: str = None):
    valid_voices = {k: VOICES_CONFIG[k] for k in allowed_voices if k in VOICES_CONFIG}
    if not valid_voices:
        valid_voices = VOICES_CONFIG

    voice_names = list(valid_voices.keys())
    weights = [info["weight"] for info in valid_voices.values()]
    selected = random.choices(voice_names, weights=weights)[0]
    
    if last_voice and len(voice_names) > 1:
        for _ in range(3):
            if selected != last_voice:
                break
            selected = random.choices(voice_names, weights=weights)[0]
            
    return selected, valid_voices[selected]

def calculate_final_rate(base_rate: int, offset_rate: int) -> str:
    final_val = base_rate + offset_rate
    return f"+{final_val}%" if final_val >= 0 else f"{final_val}%"

async def generate_audio_chunk(text: str, voice: str, volume: str, rate: str, filepath: str):
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(text, voice, volume=volume, rate=rate)
            await communicate.save(filepath)
            return True
        except Exception as e:
            if attempt == 2:
                raise e
            await asyncio.sleep(1)

# ================= 后台排队生成 MP3 文件任务 =================

async def run_tts_background_task(task_id: str, text: str, allowed_voices: List[str], rate_offset: int):
    TASKS_DB[task_id]["status"] = "queued"
    TASKS_DB[task_id]["progress"] = "排队等待中 (前面有其他任务正在处理)..."
    save_tasks_to_disk()

    async with TTS_SEMAPHORE:
        start_time = time.time()
        TASKS_DB[task_id]["status"] = "processing"
        save_tasks_to_disk()

        parts = split_into_parts(text, max_part_size=PART_CHAR_LIMIT)
        num_parts = len(parts)
        output_files = []

        try:
            for part_idx, part_text in enumerate(parts):
                chunks = split_part_into_chunks(part_text)
                temp_files = []
                last_voice = None

                for chunk_idx, chunk in enumerate(chunks):
                    TASKS_DB[task_id]["progress"] = f"正在合成 第 {part_idx + 1}/{num_parts} 卷，段落 [{chunk_idx + 1}/{len(chunks)}]"
                    save_tasks_to_disk()
                    
                    selected_voice, conf = get_next_voice(allowed_voices, last_voice)
                    last_voice = selected_voice
                    final_rate = calculate_final_rate(conf["base_rate"], rate_offset)
                    
                    temp_file = os.path.join(TTS_DIR, f"temp_{task_id}_p{part_idx}_c{chunk_idx}.mp3")
                    await generate_audio_chunk(chunk, selected_voice, conf["volume"], final_rate, temp_file)
                    
                    if os.path.exists(temp_file):
                        temp_files.append(temp_file)
                    await asyncio.sleep(0.1)

                part_filename = f"{task_id}_part{part_idx + 1}.mp3" if num_parts > 1 else f"{task_id}.mp3"
                part_filepath = os.path.join(TTS_DIR, part_filename)

                with open(part_filepath, 'wb') as outfile:
                    for tf in temp_files:
                        if os.path.exists(tf):
                            with open(tf, 'rb') as infile:
                                outfile.write(infile.read())
                            os.remove(tf)

                output_files.append({
                    "part": part_idx + 1,
                    "title": f"第 {part_idx + 1} 卷 / 共 {num_parts} 卷" if num_parts > 1 else "完整音频",
                    "filename": part_filename,
                    "url": f"/static/mp3_tts/{part_filename}"
                })

            elapsed = round(time.time() - start_time, 1)
            TASKS_DB[task_id].update({
                "status": "success",
                "progress": "合成完成！",
                "elapsed": elapsed,
                "files": output_files
            })
            save_tasks_to_disk()

        except Exception as e:
            TASKS_DB[task_id].update({
                "status": "error",
                "error_message": f"合成失败: {str(e)}"
            })
            save_tasks_to_disk()

# ================= FastAPI 路由 =================

app = FastAPI(title="Edge-TTS Full Service")
app.mount("/static", StaticFiles(directory="static"), name="static")

class TTSRequest(BaseModel):
    text: str
    selected_voices: List[str] = []
    rate_offset: int = 0
    force_submit: bool = False

class StreamChunkRequest(BaseModel):
    text: str
    voice: str
    rate_offset: int = 0

@app.get("/")
async def serve_index():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="未找到 templates/index.html 文件！")
    return FileResponse(index_path)

@app.get("/api/config")
async def get_config():
    return {
        "voices": VOICES_CONFIG,
        "part_char_limit": PART_CHAR_LIMIT,
        "chunk_target_size": CHUNK_TARGET_SIZE
    }

# 1. 后台长文本生成 MP3 接口
@app.post("/api/tts")
async def tts_endpoint(req: TTSRequest):
    text = clean_text(req.text)
    char_count = len(text)

    if char_count == 0:
        raise HTTPException(status_code=400, detail="输入的文本为空！")

    allowed_voices = req.selected_voices if req.selected_voices else list(VOICES_CONFIG.keys())
    parts = split_into_parts(text, max_part_size=PART_CHAR_LIMIT)
    num_parts = len(parts)

    if num_parts > 1 and not req.force_submit:
        est_minutes = round(char_count / 300, 1)
        return {
            "status": "warning",
            "message": f"文本共 {char_count} 字（预计 {est_minutes} 分钟）。\n将自动切分为 {num_parts} 个音频文件（每卷约 30 分钟）进行输出。\n是否开始提交后台排队合成？",
            "char_count": char_count,
            "num_parts": num_parts
        }

    task_id = str(uuid.uuid4())[:8]
    TASKS_DB[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "progress": "进入队列中...",
        "char_count": char_count,
        "num_parts": num_parts,
        "elapsed": 0,
        "files": [],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    save_tasks_to_disk()

    asyncio.create_task(run_tts_background_task(task_id, text, allowed_voices, req.rate_offset))

    return {
        "status": "started",
        "task_id": task_id,
        "message": "已提交后台处理！"
    }

@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    if task_id not in TASKS_DB:
        raise HTTPException(status_code=404, detail="未找到该任务")
    return TASKS_DB[task_id]

# 2. “立即听书”专用单句秒播接口（不落盘）
@app.post("/api/tts_stream")
async def tts_stream_endpoint(req: StreamChunkRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="文本为空")

    conf = VOICES_CONFIG.get(req.voice, VOICES_CONFIG["zh-CN-XiaoxiaoNeural"])
    final_rate = calculate_final_rate(conf["base_rate"], req.rate_offset)

    communicate = edge_tts.Communicate(req.text, req.voice, volume=conf["volume"], rate=final_rate)
    
    audio_bytes = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_bytes.extend(chunk["data"])

    return Response(content=bytes(audio_bytes), media_type="audio/mpeg")

# 3. 运维 curl 清理接口
@app.post("/api/admin/clean-mp3")
async def clean_mp3_files(token: str = "", days: int = 7):
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="鉴权失败：无效的 Admin Token")

    now = time.time()
    cutoff_time = now - (days * 86400)
    deleted_count = 0
    freed_bytes = 0

    for filename in os.listdir(TTS_DIR):
        filepath = os.path.join(TTS_DIR, filename)
        if os.path.isfile(filepath) and filename.endswith(".mp3") and not filename.startswith("temp_"):
            file_mtime = os.path.getmtime(filepath)
            if days == 0 or file_mtime < cutoff_time:
                file_size = os.path.getsize(filepath)
                try:
                    os.remove(filepath)
                    deleted_count += 1
                    freed_bytes += file_size
                except Exception as e:
                    print(f"删除失败: {e}")

    return {
        "status": "success",
        "message": f"清理完成！共删除 {deleted_count} 个音频文件。",
        "freed_mb": round(freed_bytes / (1024 * 1024), 2)
    }