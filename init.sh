#!/bin/sh

# pip install --no-cache-dir -r requirements.txt
pip3 install  -i https://pypi.tuna.tsinghua.edu.cn/simple   gunicorn==20.1.0

# 2026.03.25
pip3 install  -i https://pypi.tuna.tsinghua.edu.cn/simple   edge_tts

# 2027.07.24
pip3 install  -i https://pypi.tuna.tsinghua.edu.cn/simple   fastapi
pip3 install  -i https://pypi.tuna.tsinghua.edu.cn/simple   uvicorn



apk add htop
apk add curl

cd /datav

/datav/run_python.sh &

# uvicorn app:app --host 0.0.0.0 --port 80 --reload

gunicorn -c /datav/gunicorn-cfg.py app:app