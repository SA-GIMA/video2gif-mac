#!/bin/bash
cd "$(dirname "$0")"

echo "================================"
echo "  视频转 GIF 工具"
echo "================================"
echo ""

# 检查依赖
if ! command -v python3 &>/dev/null; then
    echo "[错误] 未找到 python3，请先安装 Python 3。"
    echo "按任意键退出..."
    read -n 1
    exit 1
fi

if ! command -v ffmpeg &>/dev/null; then
    echo "[错误] 未找到 ffmpeg，请先安装 ffmpeg。"
    echo "  安装方法: brew install ffmpeg"
    echo "按任意键退出..."
    read -n 1
    exit 1
fi

# 检查 Flask
if ! python3 -c "import flask" 2>/dev/null; then
    echo "[错误] 未安装 Flask，请执行: pip3 install flask"
    echo "按任意键退出..."
    read -n 1
    exit 1
fi

# 创建目录
mkdir -p uploads output

echo "正在启动服务器 http://127.0.0.1:5050 ..."
echo "浏览器将自动打开，如未打开请手动访问上述地址。"
echo ""
echo "按 Ctrl+C 停止服务。"
echo ""

# 延迟后自动打开浏览器
(sleep 1.5 && open "http://127.0.0.1:5050") &

# 启动服务
python3 server.py
