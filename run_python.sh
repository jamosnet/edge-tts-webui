#!/bin/sh

while true; do
    # 直接在获取时间时，通过 $((10#...)) 强制转换为十进制整数
    # 这样 minute 和 hour 变量中存放的就是纯数字（例如 0、8、20），不再有前导零和八进制问题
    minute=$((10#$(date +%M)))
    hour=$((10#$(date +%H)))

    # 规则 3：每 20 分钟运行一次
    if [ $((minute % 20)) -eq 0 ]; then  
        cd /datav
        timeout -k 10s 60s curl -s -X POST "http://127.0.0.1/api/admin/clean-mp3?token=my_admin_secret_123&days=3" >/dev/null 2>&1 &
    fi
    
    # 每次循环固定等待 60 秒
    sleep 60
done