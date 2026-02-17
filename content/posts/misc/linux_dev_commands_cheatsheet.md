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
# 删除未跟踪文件
git clean -f

# 删除未跟踪文件和目录
git clean -fd

# 删除未跟踪文件、目录和被 .gitignore 忽略的文件
git clean -xfd
```

**使用场景**: 构建产物污染工作区、切换分支前清理临时文件。

### 解决冲突的标准流程

```bash
# 暂存本地修改
git stash

# 拉取远程更新
git pull

# 恢复本地修改（可能产生冲突）
git stash pop
```

**使用场景**: 本地有未提交修改时需要同步远程代码。

### 克隆包含子模块的仓库

```bash
# 一次性克隆主仓库和所有子模块
git clone --recursive <repository_url>

# 已克隆仓库后初始化子模块
git submodule update --init --recursive
```

### 撤销与还原

```bash
# 重置到指定提交（危险操作，会丢失本地修改）
git reset --hard <commit_hash>

# 还原单个文件到最新提交状态
git checkout -- <file_path>

# 还原整个目录
git checkout -- <directory_path>
```

**注意**: `reset --hard` 会永久丢失未提交的修改，执行前务必确认。

### 批量更新多个仓库（改进版）

```bash
#!/bin/bash
# update_all_repos.sh - 批量更新当前目录下所有 Git 仓库

set -euo pipefail

LOG_FILE="update_repos_$(date +%Y%m%d_%H%M%S).log"
FAILED_REPOS=()

# 查找所有 .git 目录
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

# 汇总报告
echo "========================================" | tee -a "$LOG_FILE"
echo "Update completed. Log saved to: $LOG_FILE" | tee -a "$LOG_FILE"
if [ ${#FAILED_REPOS[@]} -gt 0 ]; then
    echo "Failed repositories:" | tee -a "$LOG_FILE"
    printf '%s\n' "${FAILED_REPOS[@]}" | tee -a "$LOG_FILE"
    exit 1
fi
```

**改进点**:
- 错误处理: `set -euo pipefail` 严格模式
- 日志记录: 时间戳日志文件
- 失败追踪: 记录失败的仓库列表
- 可读性: 使用 `mapfile` 替代管道

**并行执行版本**:

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

**使用场景**: 管理多个项目仓库的工作区，定期同步上游更新。

## 文件与磁盘管理

### 查找大文件

```bash
# 查找大于 100M 的文件
sudo find . -type f -size +100M -print0 | xargs -0 ls -lh

# 按大小排序显示前 20 个文件
sudo find . -type f -exec du -h {} + | sort -rh | head -n 20
```

**使用场景**: 磁盘空间不足时快速定位大文件。

### 磁盘使用分析

```bash
# 当前目录各子目录占用空间（按大小排序）
du -h --max-depth=1 | sort -rh

# 交互式磁盘使用分析工具（需安装 ncdu）
sudo apt install ncdu
ncdu /

# 查看文件系统挂载点和使用率
df -h
```

### 日志清理

```bash
# 清理 systemd journal 日志（保留最近 7 天）
sudo journalctl --vacuum-time=7d

# 清理 apt 缓存
sudo apt clean
sudo apt autoclean

# 清理旧内核（Ubuntu）
sudo apt autoremove --purge

# 查找并删除超过 30 天的日志文件
find /var/log -type f -name "*.log" -mtime +30 -exec rm -f {} \;
```

### 文件搜索与过滤

```bash
# grep 排除特定目录
grep -r "pattern" --exclude-dir={.git,node_modules,build}

# 查找特定类型文件
find . -type f -name "*.cpp"
find . -type f \( -name "*.h" -o -name "*.hpp" \)

# 复制文件排除特定类型
rsync -av --exclude='*.o' --exclude='*.a' src/ dest/
```

**使用场景**: 代码搜索、构建产物过滤、备份时排除临时文件。

## 系统信息与监控

### 查看 GLIBC 和 GLIBCXX 版本

```bash
# 查看系统 GLIBC 版本
ldd --version

# 查看可执行文件依赖的 GLIBCXX 版本
strings /usr/lib/x86_64-linux-gnu/libstdc++.so.6 | grep GLIBCXX
```

**使用场景**: 排查动态链接库版本不兼容问题。

### CPU 信息查看

```bash
# 查看 CPU 详细信息
lscpu

# 查看 CPU 核心数
nproc

# 查看 CPU 实时使用率
top
htop  # 更友好的交互式界面
```

### GPU 监控

```bash
# NVIDIA GPU 实时监控
watch -n 1 nvidia-smi

# 持续记录 GPU 使用情况到日志
nvidia-smi --query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total \
    --format=csv -l 5 >> gpu_monitor.log
```

**使用场景**: 深度学习训练监控、GPU 资源分配调试。

### 进程与端口管理

```bash
# 查看占用特定端口的进程
sudo lsof -i :8080
sudo netstat -tulnp | grep 8080

# 杀死占用端口的进程
sudo kill -9 $(sudo lsof -t -i:8080)

# 查看进程树
pstree -p
```

## 网络诊断与测试

### 网络连通性检测脚本（改进版）

```bash
#!/bin/bash
# network_monitor.sh - 网络连通性监控与告警

set -euo pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 <interface> <target_ip>"
    echo "Example: $0 eth0 8.8.8.8"
    exit 1
fi

INTERFACE=$1
TARGET_IP=$2
LOG_DIR="/var/log/network_monitor"
LOG_FILE="$LOG_DIR/monitor_$(date +%Y%m%d).log"
ALERT_FILE="$LOG_DIR/alerts.log"
FAIL_COUNT=0
ALERT_THRESHOLD=3

# 创建日志目录
mkdir -p "$LOG_DIR"

log_message() {
    local level=$1
    local message=$2
    local timestamp=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$timestamp] [$level] $message" | tee -a "$LOG_FILE"
}

send_alert() {
    local message=$1
    echo "[$(date)] ALERT: $message" >> "$ALERT_FILE"
    # 可扩展: 发送邮件、企业微信、钉钉通知
    # curl -X POST <webhook_url> -d "{\"text\":\"$message\"}"
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

# 主循环
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

**改进点**:
- 日志分级: INFO/ERROR
- 告警机制: 连续失败达到阈值触发告警
- 日志轮转: 按日期分割日志文件
- 可扩展性: 预留 webhook 通知接口

**systemd service 配置**:

```ini
# /etc/systemd/system/network-monitor.service
[Unit]
Description=Network Connectivity Monitor
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/network_monitor.sh eth0 8.8.8.8
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

启用服务:

```bash
sudo systemctl daemon-reload
sudo systemctl enable network-monitor.service
sudo systemctl start network-monitor.service
sudo systemctl status network-monitor.service
```

### cURL 测试

```bash
# 测试 HTTP 接口
curl -X POST http://localhost:8080/api/test \
    -H "Content-Type: application/json" \
    -d '{"key":"value"}'

# 显示详细请求信息
curl -v http://example.com

# 测试响应时间
curl -w "@curl-format.txt" -o /dev/null -s http://example.com

# curl-format.txt 内容:
# time_namelookup:  %{time_namelookup}\n
# time_connect:     %{time_connect}\n
# time_starttransfer: %{time_starttransfer}\n
# time_total:       %{time_total}\n
```

### Apache Bench 压力测试

```bash
# 100 并发，总共 10000 请求
ab -n 10000 -c 100 http://localhost:8080/

# POST 请求压测
ab -n 1000 -c 10 -p data.json -T application/json http://localhost:8080/api
```

## Docker 容器管理

### 容器批量操作

```bash
# 停止所有运行中的容器
docker stop $(docker ps -q)

# 删除所有已停止的容器
docker rm $(docker ps -aq)

# 删除所有未使用的镜像
docker image prune -a

# 清理所有未使用的资源（容器、网络、镜像、构建缓存）
docker system prune -a --volumes
```

### 容器信息查询

```bash
# 查看容器 IP 地址
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <container_name>

# 查看容器资源使用情况
docker stats

# 查看容器日志（最近 100 行）
docker logs --tail 100 -f <container_name>
```

### 镜像导入导出

```bash
# 导出镜像
docker save -o image.tar <image_name>:<tag>

# 导入镜像
docker load -i image.tar

# 导出容器为镜像
docker export <container_id> > container.tar
docker import container.tar <new_image_name>:<tag>
```

**使用场景**: 离线环境部署、镜像备份迁移。

## FFmpeg 多媒体处理

### 流媒体推送

```bash
# RTMP 推流
ffmpeg -re -i input.mp4 -c copy -f flv rtmp://server/live/stream

# RTSP 推流
ffmpeg -re -i input.mp4 -c:v libx264 -preset ultrafast -c:a aac -f rtsp rtsp://server:8554/stream
```

### 视频格式转换

```bash
# MP4 转 H.264 编码
ffmpeg -i input.mp4 -c:v libx264 -preset medium -crf 23 -c:a aac -b:a 128k output.mp4

# 提取音频
ffmpeg -i input.mp4 -vn -c:a copy output.aac

# 批量转换当前目录所有 AVI 为 MP4
for file in *.avi; do
    ffmpeg -i "$file" -c:v libx264 -crf 23 "${file%.avi}.mp4"
done
```

### 调试与分析

```bash
# 查看媒体文件详细信息
ffprobe -v error -show_format -show_streams input.mp4

# 提取关键帧
ffmpeg -i input.mp4 -vf "select='eq(pict_type,I)'" -vsync vfr frame_%04d.png
```

**使用场景**: 视频监控系统、流媒体服务器、多媒体处理管道。

## 编译与构建

### 多核编译

```bash
# 使用所有 CPU 核心编译
make -j$(nproc)

# CMake 构建时指定并行度
cmake --build build -- -j$(nproc)

# 限制最大并行数（避免内存不足）
make -j$(($(nproc) / 2))
```

### 后台运行与日志

```bash
# nohup 后台运行并记录日志
nohup ./long_running_task > output.log 2>&1 &

# 查看后台任务
jobs
ps aux | grep long_running_task

# 将前台任务转到后台
Ctrl+Z  # 暂停任务
bg      # 后台继续运行
```

## 会话管理

### tmux 常用操作

```bash
# 创建新会话
tmux new -s session_name

# 列出所有会话
tmux ls

# 附加到会话
tmux attach -t session_name

# 分离会话（在 tmux 内按键）
Ctrl+b d

# 杀死会话
tmux kill-session -t session_name

# 水平分割窗口
Ctrl+b %

# 垂直分割窗口
Ctrl+b "

# 切换窗格
Ctrl+b 方向键
```

### screen 常用操作

```bash
# 创建新会话
screen -S session_name

# 列出所有会话
screen -ls

# 附加到会话
screen -r session_name

# 分离会话（在 screen 内按键）
Ctrl+a d

# 杀死会话
screen -X -S session_name quit
```

**使用场景**: SSH 连接不稳定时保持任务运行、多任务并行监控。

## 自动化脚本

### 循环执行命令

```bash
# 每 5 秒执行一次
while true; do
    ./your_command
    sleep 5
done

# 使用 watch 命令（更简洁）
watch -n 5 ./your_command
```

### 开机延迟启动脚本（Linux 版）

```bash
#!/bin/bash
# delayed_startup.sh - 开机延迟启动服务

sleep 10  # 延迟 10 秒

# 启动应用
cd /opt/myapp
./myapp &

# 记录启动日志
echo "$(date): Application started" >> /var/log/delayed_startup.log
```

**systemd service 配置**:

```ini
# /etc/systemd/system/delayed-startup.service
[Unit]
Description=Delayed Application Startup
After=network.target

[Service]
Type=forking
ExecStart=/usr/local/bin/delayed_startup.sh
User=appuser
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### 用户管理

```bash
# 创建新用户（交互式）
sudo adduser username

# 创建系统用户（非交互式）
sudo useradd -r -s /bin/false serviceuser

# 添加用户到 sudo 组
sudo usermod -aG sudo username

# 删除用户及其主目录
sudo userdel -r username
```

## 最佳实践建议

1. **脚本健壮性**: 使用 `set -euo pipefail` 严格模式，避免静默失败
2. **日志记录**: 关键操作记录时间戳日志，便于问题追溯
3. **错误处理**: 检查命令返回值，提供有意义的错误信息
4. **资源清理**: 使用 trap 捕获信号，确保临时文件和进程被清理
5. **权限最小化**: 避免不必要的 sudo，使用专用服务账户运行服务
6. **配置外部化**: 敏感信息（IP、密码）通过环境变量或配置文件传入
7. **版本控制**: 将常用脚本纳入 Git 管理，记录修改历史
8. **文档化**: 脚本头部注释说明用途、参数、依赖和示例

## 参考资源

- [Advanced Bash-Scripting Guide](https://tldp.org/LDP/abs/html/)
- [systemd.service — Service unit configuration](https://www.freedesktop.org/software/systemd/man/systemd.service.html)
- [FFmpeg Documentation](https://ffmpeg.org/documentation.html)
- [Docker CLI Reference](https://docs.docker.com/engine/reference/commandline/cli/)
