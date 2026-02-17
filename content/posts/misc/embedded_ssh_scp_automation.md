---
title: "告别手动输密码: 嵌入式 SSH/SCP 自动化方案"
date: 2026-02-16T12:00:00
draft: false
categories: ["misc"]
tags: ["SSH", "SCP", "expect", "rsync", "ssh-config", "ProxyJump", "embedded", "Linux", "automation", "security"]
summary: "嵌入式 Linux 开发中频繁需要在宿主机和目标板之间传输文件、远程调试。本文从实际需求出发，对比 Expect 脚本、sshpass、SSH 密钥认证三种自动化方案的实现与安全性，然后介绍 SSH Config、ProxyJump 跳板机、rsync 增量同步等进阶技术，构建一套完整的嵌入式远程开发工具链。"
ShowToc: true
TocOpen: true
---

> 原文链接: [Linux Shell: 使用 Expect 自动化 SCP 和 SSH 连接的 Shell 脚本详解](https://blog.csdn.net/stallion5632/article/details/142489557)

## 1. 问题场景

嵌入式 Linux 开发的典型工作流：

```
宿主机 (x86 Ubuntu)                  目标板 (ARM Linux)
┌──────────────────┐    SSH/SCP     ┌──────────────────┐
│  交叉编译         │ ──────────→   │  /opt/app/        │
│  cmake --build   │               │  运行、调试        │
│  单元测试         │ ←────────── │  日志、core dump   │
└──────────────────┘    SCP/rsync   └──────────────────┘
```

每次编译后需要将二进制文件 SCP 到目标板、SSH 登录重启服务、拉取日志回宿主机分析。手动输入密码的重复操作在日均数十次的开发迭代中严重影响效率。

## 2. 方案一: Expect 自动化

Expect 是基于 Tcl 的交互自动化工具，通过模式匹配和自动应答实现非交互式操作。

### 2.1 SSH 自动登录

```bash
#!/bin/bash
# ssh_auto.sh
export TERM=xterm-256color
ip='192.168.1.10'
password='your_password'

ssh-keygen -f "$HOME/.ssh/known_hosts" -R "${ip}" 2>/dev/null

expect -c '
  set timeout 10
  set password "'"$password"'"
  spawn ssh -o StrictHostKeyChecking=no root@'"$ip"'
  expect {
    "*yes/no*" { send "yes\r"; exp_continue }
    "*password:*" { send "$password\r"; exp_continue }
    eof
  }
  interact
'
```

### 2.2 SCP 上传/下载

```bash
# 上传
expect -c '
  set timeout 30
  set password "'"$password"'"
  spawn scp -o StrictHostKeyChecking=no '"$file"' root@'"$ip"':'"$dest"'
  expect {
    "*password:*" { send "$password\r"; exp_continue }
    eof
  }
'

# 下载
expect -c '
  spawn scp -o StrictHostKeyChecking=no root@'"$ip"':'"$remote_file"' '"$dest"'
  expect {
    "*password:*" { send "$password\r"; exp_continue }
    eof
  }
'
```

### 2.3 局限性

| 问题 | 说明 |
|------|------|
| 密码明文 | 硬编码在脚本中，任何有读权限的用户都能看到 |
| 进程可见 | `ps aux` 可能显示命令行中的密码 |
| 多层转义 | Bash + Tcl 双层引号处理，密码含特殊字符时极易出错 |
| 无断点续传 | SCP 传输中断后必须重新开始 |
| 绕过主机验证 | `StrictHostKeyChecking=no` 禁用了中间人攻击防护 |

Expect 适合临时使用或密码无法更改的遗留系统。

## 3. 方案二: sshpass

sshpass 是专为 SSH 密码自动化设计的工具，比 Expect 更简洁：

```bash
# 安装
sudo apt install sshpass

# SSH 登录
sshpass -p 'your_password' ssh root@192.168.1.10

# SCP 上传/下载
sshpass -p 'your_password' scp ./app root@192.168.1.10:/opt/
sshpass -p 'your_password' scp root@192.168.1.10:/var/log/app.log ./
```

### 3.1 密码传递的安全层级

| 方式 | 命令 | 安全性 | 风险 |
|------|------|:------:|------|
| `-p` 命令行 | `sshpass -p 'pwd' ssh ...` | 最低 | `ps` 和 shell history 可见 |
| `-f` 文件 | `sshpass -f /path/to/pwfile ssh ...` | 中等 | 文件权限需设为 `0400` |
| `-e` 环境变量 | `SSHPASS=pwd sshpass -e ssh ...` | 较高 | 用完后应 `unset SSHPASS` |

推荐文件方式：

```bash
echo 'your_password' > ~/.ssh/.board_pwd
chmod 0400 ~/.ssh/.board_pwd
sshpass -f ~/.ssh/.board_pwd ssh root@192.168.1.10
```

### 3.2 sshpass vs Expect

| 维度 | sshpass | Expect |
|------|:-------:|:------:|
| SSH/SCP 专用 | 是 | 通用交互自动化 |
| 代码量 | 一行命令 | 10+ 行脚本 |
| 特殊字符处理 | 无需额外转义 | Bash + Tcl 双层转义 |
| 复杂交互 (sudo, 菜单) | 不支持 | 支持 |

仅自动化 SSH/SCP 密码输入时，sshpass 比 Expect 更简洁。

## 4. 方案三: SSH 密钥认证 (推荐)

SSH 公钥认证是自动化登录的工业标准方案。无密码传输、无明文存储、抗暴力破解。

### 4.1 密钥生成与部署

```bash
# 1. 生成 Ed25519 密钥对
ssh-keygen -t ed25519 -C "dev@workstation" -f ~/.ssh/id_ed25519_board

# 2. 部署公钥到目标板
ssh-copy-id -i ~/.ssh/id_ed25519_board.pub root@192.168.1.10

# 3. 验证免密登录
ssh -i ~/.ssh/id_ed25519_board root@192.168.1.10
```

### 4.2 权限要求

| 路径 | 权限 | 说明 |
|------|:----:|------|
| `~/.ssh/` | `700` | 仅所有者可读写执行 |
| `~/.ssh/authorized_keys` | `600` | 仅所有者可读写 |
| `~/.ssh/id_ed25519` (私钥) | `600` | 仅所有者可读写 |

### 4.3 ssh-agent 管理密钥

如果私钥设置了 passphrase，使用 ssh-agent 在会话期间缓存解密后的私钥：

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519_board
# 后续所有 SSH/SCP 操作自动使用缓存的密钥
```

### 4.4 安全性对比

| 维度 | Expect / sshpass | SSH 密钥 |
|------|:----------------:|:--------:|
| 密码/密钥传输 | 每次通过网络传输密码 | 仅传输公钥签名，私钥不离开宿主机 |
| 暴力破解 | 可被穷举 | Ed25519: 2^128 安全级别 |
| 中间人攻击 | `StrictHostKeyChecking=no` 禁用防护 | 主机密钥验证 + 密钥签名双重保护 |
| 明文存储 | 脚本/文件中存在明文密码 | 私钥可加密 (passphrase) |
| 凭证泄露影响 | 密码泄露 = 完全控制 | 公钥泄露无影响，私钥泄露可立即吊销 |

## 5. SSH Config: 统一管理多台设备

在 `~/.ssh/config` 中配置别名，避免记忆 IP 和参数：

```ssh
# ~/.ssh/config

Host board
    HostName 192.168.1.10
    User root
    IdentityFile ~/.ssh/id_ed25519_board
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new

Host lab-gateway
    HostName 10.0.0.1
    User engineer
    IdentityFile ~/.ssh/id_ed25519_work

Host lab-board
    HostName 192.168.100.10
    User root
    ProxyJump lab-gateway
    IdentityFile ~/.ssh/id_ed25519_board

Host *
    AddKeysToAgent yes
    ServerAliveInterval 60
    ServerAliveCountMax 3
    ConnectTimeout 10
```

配置后的使用：

```bash
ssh board                          # 自动查找 IP、用户名、密钥
scp ./app board:/opt/              # SCP 上传
ssh lab-board                      # 通过跳板机登录实验室目标板
scp ./firmware.bin lab-board:/tmp/ # 通过跳板机 SCP
```

### 5.1 关键配置项

| 配置项 | 说明 |
|--------|------|
| `IdentitiesOnly yes` | 仅使用指定的密钥文件，不尝试 agent 中的其他密钥 |
| `StrictHostKeyChecking accept-new` | 首次连接自动接受并保存主机密钥，后续连接验证 |
| `ServerAliveInterval 60` | 每 60 秒发送心跳，防止 NAT 超时断连 |
| `AddKeysToAgent yes` | 首次使用密钥时自动添加到 ssh-agent |

### 5.2 ProxyJump 跳板机

ProxyJump 实现端到端加密的跳板连接，私钥始终留在宿主机上：

```bash
# 命令行方式
ssh -J engineer@10.0.0.1 root@192.168.100.10
scp -J engineer@10.0.0.1 ./app root@192.168.100.10:/opt/

# 多级跳板
ssh -J bastion1,bastion2 root@target
```

| 方式 | 私钥位置 | 安全风险 |
|------|---------|---------|
| `ProxyJump` | 始终在宿主机 | 跳板机无法窃取私钥 |
| `ForwardAgent yes` | 转发到跳板机 | 跳板机 root 用户可劫持 agent |

推荐使用 ProxyJump。

## 6. rsync: 增量同步

rsync 使用 delta 算法，仅传输变化的部分。

### 6.1 基本用法

```bash
# 同步目录到目标板 (增量传输)
rsync -avz --progress ./build/output/ board:/opt/app/

# 从目标板拉取日志
rsync -avz board:/var/log/app/ ./logs/

# 通过跳板机同步
rsync -avz ./build/output/ lab-board:/opt/app/
```

### 6.2 常用选项

| 选项 | 说明 |
|------|------|
| `-a` | 归档模式: 递归、保留权限/时间戳/符号链接 |
| `-v` | 显示传输过程 |
| `-z` | 传输时压缩 |
| `--progress` | 显示进度 |
| `--delete` | 删除目标端多余的文件 (镜像同步) |
| `--exclude '*.o'` | 排除中间文件 |

### 6.3 性能对比

| 场景 | SCP | rsync |
|------|:---:|:-----:|
| 首次传输 10 MB 二进制 | 1.5 s | 1.8 s (校验开销) |
| 修改 1 KB 后重新传输 | 1.5 s (全量) | 0.3 s (增量) |
| 传输中断后恢复 | 从头开始 | `--partial` 断点续传 |

嵌入式迭代开发中，rsync 的增量传输可以将部署时间缩短 80% 以上。

## 7. 实用脚本: 一键部署

```bash
#!/bin/bash
# deploy.sh -- 编译并部署到目标板
# 用法: ./deploy.sh [board|lab-board]

set -euo pipefail

TARGET=${1:-board}
BUILD_DIR="./build"
REMOTE_DIR="/opt/app"
BINARY="my_app"

echo "=== Building ==="
cmake --build "$BUILD_DIR" --target "$BINARY" -j"$(nproc)"

echo "=== Deploying to $TARGET ==="
rsync -avz --progress \
    --exclude '*.o' \
    --exclude 'CMakeFiles' \
    "$BUILD_DIR/$BINARY" \
    "$TARGET:$REMOTE_DIR/"

echo "=== Restarting service ==="
ssh "$TARGET" "systemctl restart my_app || $REMOTE_DIR/$BINARY &"

echo "=== Done ==="
```

这个脚本不包含任何密码、IP 地址或密钥路径，所有连接参数由 `~/.ssh/config` 管理。

## 8. 方案选择决策树

```
需要自动化 SSH/SCP?
│
├── 能否部署密钥到目标板?
│   ├── 是 → SSH 密钥认证 + SSH Config + rsync (推荐)
│   └── 否 (权限/策略限制)
│       ├── 能否安装 sshpass?
│       │   ├── 是 → sshpass -f (文件方式)
│       │   └── 否 → Expect 脚本 (最后手段)
│       └── 需要复杂交互 (sudo/菜单)?
│           └── 是 → Expect 脚本
│
├── 需要通过跳板机?
│   └── SSH Config + ProxyJump
│
├── 频繁传输文件?
│   ├── 目标板有 rsync → rsync -avz
│   └── 目标板无 rsync → scp
│
└── 安全性要求高?
    └── SSH 密钥 + StrictHostKeyChecking + 禁用 PasswordAuthentication
```

## 9. 安全加固

生产环境建议：禁用目标板密码登录 (`/etc/ssh/sshd_config: PasswordAuthentication no`)、使用 Ed25519 密钥、启用主机密钥验证 (`StrictHostKeyChecking accept-new`)、为私钥设置 passphrase。
