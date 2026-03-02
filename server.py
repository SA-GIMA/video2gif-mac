import os
import uuid
import json
import time
import subprocess
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from flask import (
    Flask, request, jsonify, send_from_directory, render_template, Response
)
from werkzeug.utils import secure_filename

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

ALLOWED_EXTENSIONS = {"mp4", "avi", "mov", "mkv", "webm", "flv", "wmv"}
MAX_FILES = 10
FFMPEG_TIMEOUT = 600  # 10 分钟

executor = ThreadPoolExecutor(max_workers=3)

# task_id -> {status, progress, output, error, filename}
tasks: dict[str, dict] = {}
tasks_lock = threading.Lock()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_video_duration(filepath: str) -> float | None:
    """使用 ffprobe 获取视频时长（秒）。"""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath,
            ],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def parse_time_seconds(time_str: str) -> float:
    """将 HH:MM:SS.ms 或纯秒数字符串解析为浮点秒数。"""
    match = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", time_str)
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)
    try:
        return float(time_str)
    except ValueError:
        return 0.0


def update_task(task_id: str, **kwargs):
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id].update(kwargs)


def run_conversion(task_id: str, input_path: str, params: dict):
    """在后台线程中执行 ffmpeg 转换。"""
    output_name = f"{task_id}.gif"
    output_path = str(OUTPUT_DIR / output_name)
    palette_path = str(OUTPUT_DIR / f"{task_id}_palette.png")

    fps = params.get("fps", 10)
    width = params.get("width", 480)
    start = params.get("start", 0)
    duration = params.get("duration", 0)
    quality = params.get("quality", "high")
    loop = params.get("loop", 0)

    # 构建缩放滤镜
    vf_scale = f"scale={width}:-1:flags=lanczos"

    # 时间参数
    time_args = []
    if start and float(start) > 0:
        time_args += ["-ss", str(start)]
    if duration and float(duration) > 0:
        time_args += ["-t", str(duration)]

    # 获取总时长用于进度计算
    total_duration = get_video_duration(input_path)
    if duration and float(duration) > 0:
        total_duration = float(duration)
    elif total_duration and start and float(start) > 0:
        total_duration = total_duration - float(start)

    try:
        if quality == "high":
            # 双 pass 高质量模式：先生成调色板
            update_task(task_id, status="converting", progress=0, stage="正在生成调色板...")

            cmd1 = (
                ["ffmpeg", "-y"] + time_args +
                ["-i", input_path,
                 "-vf", f"fps={fps},{vf_scale},palettegen=stats_mode=diff",
                 "-update", "1", palette_path]
            )
            p1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
            if p1.returncode != 0:
                update_task(task_id, status="error", error=f"调色板生成失败: {p1.stderr[-500:]}")
                return

            update_task(task_id, progress=30, stage="正在使用调色板转换...")

            cmd2 = (
                ["ffmpeg", "-y"] + time_args +
                ["-i", input_path, "-i", palette_path,
                 "-lavfi", f"fps={fps},{vf_scale} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
                 "-loop", str(loop),
                 output_path]
            )
            proc = subprocess.Popen(cmd2, stderr=subprocess.PIPE, text=True)

            for line in proc.stderr:
                match = re.search(r"time=(\S+)", line)
                if match and total_duration and total_duration > 0:
                    current = parse_time_seconds(match.group(1))
                    pct = min(95, 30 + int((current / total_duration) * 65))
                    update_task(task_id, progress=pct)

            proc.wait(timeout=FFMPEG_TIMEOUT)
            if proc.returncode != 0:
                update_task(task_id, status="error", error="GIF 生成失败")
                return

            try:
                os.remove(palette_path)
            except OSError:
                pass

        else:
            # 单 pass 快速模式
            update_task(task_id, status="converting", progress=0, stage="正在转换...")

            cmd = (
                ["ffmpeg", "-y"] + time_args +
                ["-i", input_path,
                 "-vf", f"fps={fps},{vf_scale}",
                 "-loop", str(loop),
                 output_path]
            )
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)

            for line in proc.stderr:
                match = re.search(r"time=(\S+)", line)
                if match and total_duration and total_duration > 0:
                    current = parse_time_seconds(match.group(1))
                    pct = min(95, int((current / total_duration) * 95))
                    update_task(task_id, progress=pct)

            proc.wait(timeout=FFMPEG_TIMEOUT)
            if proc.returncode != 0:
                update_task(task_id, status="error", error="GIF 生成失败")
                return

        if not os.path.exists(output_path):
            update_task(task_id, status="error", error="输出文件未生成")
            return

        file_size = os.path.getsize(output_path)
        update_task(
            task_id,
            status="done",
            progress=100,
            output=output_name,
            file_size=file_size,
            stage="已完成",
        )

    except subprocess.TimeoutExpired:
        update_task(task_id, status="error", error="转换超时（单文件限制 10 分钟）")
        try:
            proc.kill()
        except Exception:
            pass
    except Exception as e:
        update_task(task_id, status="error", error=str(e))
    finally:
        try:
            os.remove(palette_path)
        except OSError:
            pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "files" not in request.files:
        return jsonify(error="未提供文件"), 400

    files = request.files.getlist("files")
    if len(files) > MAX_FILES:
        return jsonify(error=f"最多允许上传 {MAX_FILES} 个文件"), 400

    uploaded = []
    for f in files:
        if not f.filename:
            continue
        if not allowed_file(f.filename):
            return jsonify(error=f"不支持的文件格式: {f.filename}"), 400

        safe_name = secure_filename(f.filename)
        unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        save_path = UPLOAD_DIR / unique_name
        f.save(str(save_path))

        duration = get_video_duration(str(save_path))
        uploaded.append({
            "filename": unique_name,
            "original_name": f.filename,
            "size": os.path.getsize(str(save_path)),
            "duration": duration,
        })

    return jsonify(files=uploaded)


@app.route("/convert", methods=["POST"])
def convert():
    data = request.get_json()
    if not data or "files" not in data:
        return jsonify(error="未指定要转换的文件"), 400

    params = {
        "fps": max(1, min(30, int(data.get("fps", 10)))),
        "width": max(100, min(1920, int(data.get("width", 480)))),
        "start": data.get("start", 0),
        "duration": data.get("duration", 0),
        "quality": data.get("quality", "high"),
        "loop": max(0, int(data.get("loop", 0))),
    }

    task_ids = []
    for filename in data["files"]:
        safe = secure_filename(filename)
        input_path = str(UPLOAD_DIR / safe)
        if not os.path.exists(input_path):
            return jsonify(error=f"文件未找到: {filename}"), 404

        task_id = uuid.uuid4().hex[:12]
        with tasks_lock:
            tasks[task_id] = {
                "status": "queued",
                "progress": 0,
                "output": None,
                "error": None,
                "filename": filename,
                "stage": "排队中",
                "file_size": 0,
            }

        executor.submit(run_conversion, task_id, input_path, params)
        task_ids.append({"task_id": task_id, "filename": filename})

    return jsonify(tasks=task_ids)


@app.route("/status/<task_id>")
def status_sse(task_id):
    def generate():
        while True:
            with tasks_lock:
                task = tasks.get(task_id)
            if not task:
                yield f"data: {json.dumps({'error': '任务未找到'})}\n\n"
                break

            yield f"data: {json.dumps(task)}\n\n"

            if task["status"] in ("done", "error"):
                break
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/download/<filename>")
def download(filename):
    safe = secure_filename(filename)
    return send_from_directory(str(OUTPUT_DIR), safe, as_attachment=True)


@app.route("/preview/<filename>")
def preview(filename):
    safe = secure_filename(filename)
    return send_from_directory(str(OUTPUT_DIR), safe)


@app.route("/clean", methods=["DELETE"])
def clean():
    count = 0
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        for f in d.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
    with tasks_lock:
        tasks.clear()
    return jsonify(deleted=count)


def cleanup_old_files():
    """启动时清理上次残留的临时文件。"""
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        for f in d.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass


if __name__ == "__main__":
    cleanup_old_files()
    print("视频转 GIF 服务已启动: http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
