---
title: "Linux 开发常用命令与脚本速查手册"
date: 2026-02-17T11:30:00
draft: false
categories: ["misc"]
tags: ["linux", "shell", "git", "docker", "ffmpeg", "automation", "devops"]
summary: "整合 Git 操作、文件管理、系统监控、网络诊断、Docker 容器、FFmpeg 处理、自动化脚本等常用命令与最佳实践，提供开箱即用的解决方案和改进建议。"
---

## Git 操作速查

### 清理工作区

```bash
git clean -f          # 删除未跟踪文件
git clean -fd         # 删除未跟踪文件和目录
git clean -xfd        # 删除未跟踪文件、目录和被 .gitignore 忽略的文件
```

### 解决冲突的标准流程

```bash
git stash             # 暂存本地修改
git pull              # 拉取远程更新
git stash pop         # 恢复本地修改（可能产生冲突）
```

### 克隆包含子模块的仓库

```bash
git clone --recursive <repository_url>                # 一次性克隆主仓库和所有子模块
git submodule update --init --recursive               # 已克隆仓库后初始化子模块
```

### 撤销与还原

```bash
git reset --hard <commit_hash>    # 重置到指定提交（危险操作，会丢失本地修改）
git checkout -- <file_path>       # 还原单个文件到最新提交状态
git checkout -- <directory_path>  # 还原整个目录
```

### 批量更新多个仓库（改进版）

```bash
#!/bin/bash
# update_all_repos.sh - 批量更新当前目录下所有 Git 仓库

set -euo pipefail

LOG_FILE="update_repos_$(date +%Y%m%d_%H%M%S).log"
FAILED_REPOS=()

mapfile -t GIT_DIRS < <(find "$(pwd)" -type d -name ".git" -exec dirname {} \;)
echo "Found ${#GIT_DIRS[@]} repositories" | tee -a "$LOG_FILE"

for repo in "${GIT_DIRS[@]}"; do
    echo "----------------------------------------" | tee -a "$LOG_FILE"
    echo "Updating: $repo" | tee -a "$LOG_FILE"
    if cd "$repo"; then
        if git reset --hard >> "$LOG_FILE" 2>&1 && git pull >> "$LOG_FILE" 2>&1; then
            echo "✓ Success" | tee -a "$LOG_FILE"
        else
            echo "✗ Failed" | tee -a "$LOG_FILE"
            FAILED_REPOS+=("$repo")
        fi
    else
        echo "✗ Cannot access directory" | tee -a "$LOG_FILE"
        FAILED_REPOS+=("$repo")
    fi
done

echo "========================================" | tee -a "$LOG_FILE"
echo "Update completed. Log saved to: $LOG_FILE" | tee -a "$LOG_FILE"
if [ ${#FAILED_REPOS[@]} -gt 0 ]; then
    echo "Failed repositories:" | tee -a "$LOG_FILE"
    printf '%s\n' "${FAILED_REPOS[@]}" | tee -a "$LOG_FILE"
    exit 1
fi
```

并行执行版本:

```bash
#!/bin/bash
# update_all_repos_parallel.sh - 并行更新仓库

export LOG_FILE="update_repos_$(date +%Y%m%d_%H%M%S).log"

update_repo() {
    local repo=$1
    {
        echo "Updating: $repo"
        cd "$repo" && git reset --hard && git pull && echo "✓ $repo" || echo "✗ $repo"
    } >> "$LOG_FILE" 2>&1
}

export -f update_repo

find "$(pwd)" -type d -name ".git" -exec dirname {} \; | \
    xargs -P 4 -I {} bash -c 'update_repo "$@"' _ {}

echo "Update completed. Log: $LOG_FILE"
```

## 文件与磁盘管理

### 查找大文件

```bash
sudo find . -type f -size +100M -print0 | xargs -0 ls -lh    # 查找大于 100M 的文件
sudo find . -type f -exec du -h {} + | sort -rh | head -n 20 # 按大小排序显示前 20 个文件
```

### 磁盘使用分析

```bash
du -h --max-depth=1 | sort -rh    # 当前目录各子目录占用空间（按大小排序）
sudo journalctl --vacuum-time=7d  # 清理 systemd journal 日志（保留最近 7 天）
```

### 文件搜索与过滤

```bash
grep -r "pattern" --exclude-dir={.git,node_modules,build}    # grep 排除特定目录
find . -type f -name "*.cpp"                                  # 查找特定类型文件
rsync -av --exclude='*.o' --exclude='*.a' src/ dest/         # 复制文件排除特定类型
```

## 系统信息与监控

### 查看 GLIBC 和 GLIBCXX 版本

```bash
ldd --version                                                      # 查看系统 GLIBC 版本
strings /usr/lib/x86_64-linux-gnu/libstdc++.so.6 | grep GLIBCXX  # 查看 GLIBCXX 版本
```

### CPU 信息查看

```bash
lscpu     # 查看 CPU 详细信息
nproc     # 查看 CPU 核心数
htop      # 交互式 CPU 监控
```

### GPU 监控

```bash
watch -n 1 nvidia-smi    # NVIDIA GPU 实时监控
nvidia-smi --query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total \
    --format=csv -l 5 >> gpu_monitor.log    # 持续记录 GPU 使用情况到日志
```

### 进程与端口管理

```bash
sudo lsof -i :8080                      # 查看占用特定端口的进程
sudo kill -9 $(sudo lsof -t -i:8080)   # 杀死占用端口的进程
pstree -p                               # 查看进程树
```

## 网络诊断与测试

### 网络连通性检测脚本（改进版）

```bash
#!/bin/bash
# network_monitor.sh - 网络连通性监控与告警

set -euo pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 <interface> <target_ip>"
    exit 1
fi

INTERFACE=$1
TARGET_IP=$2
LOG_DIR="/var/log/network_monitor"
LOG_FILE="$LOG_DIR/monitor_$(date +%Y%m%d).log"
ALERT_FILE="$LOG_DIR/alerts.log"
FAIL_COUNT=0
ALERT_THRESHOLD=3

mkdir -p "$LOG_DIR"

log_message() {
    local level=$1
    local message=$2
    echo "[$(date "+%Y-%m-%d %H:%M:%S")] [$level] $message" | tee -a "$LOG_FILE"
}

send_alert() {
    echo "[$(date)] ALERT: $1" >> "$ALERT_FILE"
}

check_interface() {
    if ethtool "$INTERFACE" 2>/dev/null | grep -q "Link detected: yes"; then
        log_message "INFO" "Interface $INTERFACE is UP"
        return 0
    else
        log_message "ERROR" "Interface $INTERFACE is DOWN"
        return 1
    fi
}

check_connectivity() {
    if ping -c 1 -s 1024 -W 1 "$TARGET_IP" &>/dev/null; then
        log_message "INFO" "Ping $TARGET_IP success"
        FAIL_COUNT=0
        return 0
    else
        log_message "ERROR" "Ping $TARGET_IP failed"
        ((FAIL_COUNT++))
        return 1
    fi
}

log_message "INFO" "Network monitor started (interface=$INTERFACE, target=$TARGET_IP)"

while true; do
    if ! check_interface || ! check_connectivity; then
        if [ $FAIL_COUNT -ge $ALERT_THRESHOLD ]; then
            send_alert "Network connectivity lost for $FAIL_COUNT consecutive checks"
        fi
    fi
    sleep 2
done
```

### cURL 测试

```bash
curl -X POST http://localhost:8080/api/test -H "Content-Type: application/json" -d '{"key":"value"}'    # 测试 HTTP 接口
curl -v http://example.com                                                                                # 显示详细请求信息
curl -w "@curl-format.txt" -o /dev/null -s http://example.com                                            # 测试响应时间
```

### Apache Bench 压力测试

```bash
ab -n 10000 -c 100 http://localhost:8080/                                      # 100 并发，总共 10000 请求
ab -n 1000 -c 10 -p data.json -T application/json http://localhost:8080/api   # POST 请求压测
```

## Docker 容器管理

### 容器批量操作

```bash
docker stop $(docker ps -q)           # 停止所有运行中的容器
docker rm $(docker ps -aq)            # 删除所有已停止的容器
docker image prune -a                 # 删除所有未使用的镜像
docker system prune -a --volumes      # 清理所有未使用的资源
```

### 容器信息查询

```bash
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <container_name>    # 查看容器 IP 地址
docker stats                                                                                      # 查看容器资源使用情况
docker logs --tail 100 -f <container_name>                                                        # 查看容器日志（最近 100 行）
```

## FFmpeg 多媒体处理

### 流媒体推送

```bash
ffmpeg -re -i input.mp4 -c copy -f flv rtmp://server/live/stream                                      # RTMP 推流
ffmpeg -re -i input.mp4 -c:v libx264 -preset ultrafast -c:a aac -f rtsp rtsp://server:8554/stream    # RTSP 推流
```

### 视频格式转换

```bash
ffmpeg -i input.mp4 -c:v libx264 -preset medium -crf 23 -c:a aac -b:a 128k output.mp4    # MP4 转 H.264 编码
ffmpeg -i input.mp4 -vn -c:a copy output.aac                                              # 提取音频
ffprobe -v error -show_format -show_streams input.mp4                                     # 查看媒体文件详细信息
```

## 编译与构建

### 多核编译

```bash
make -j$(nproc)                       # 使用所有 CPU 核心编译
cmake --build build -- -j$(nproc)    # CMake 构建时指定并行度
make -j$(($(nproc) / 2))             # 限制最大并行数（避免内存不足）
```

### 后台运行与日志

```bash
nohup ./long_running_task > output.log 2>&1 &    # nohup 后台运行并记录日志
jobs                                              # 查看后台任务
bg                                                # 将暂停的任务转到后台继续运行
```

## 会话管理

### tmux 常用操作

```bash
tmux new -s session_name        # 创建新会话
tmux attach -t session_name     # 附加到会话
tmux kill-session -t session_name    # 杀死会话
```

### screen 常用操作

```bash
screen -S session_name          # 创建新会话
screen -r session_name          # 附加到会话
screen -X -S session_name quit  # 杀死会话
```

## 自动化脚本

### 循环执行命令

```bash
while true; do ./your_command; sleep 5; done    # 每 5 秒执行一次
watch -n 5 ./your_command                       # 使用 watch 命令（更简洁）
```

### 用户管理

```bash
sudo adduser username                    # 创建新用户（交互式）
sudo useradd -r -s /bin/false serviceuser    # 创建系统用户（非交互式）
sudo usermod -aG sudo username           # 添加用户到 sudo 组
sudo userdel -r username                 # 删除用户及其主目录
```

## 参考资源

- [Advanced Bash-Scripting Guide](https://tldp.org/LDP/abs/html/)
- [systemd.service — Service unit configuration](https://www.freedesktop.org/software/systemd/man/systemd.service.html)
- [FFmpeg Documentation](https://ffmpeg.org/documentation.html)
- [Docker CLI Reference](https://docs.docker.com/engine/reference/commandline/cli/)
