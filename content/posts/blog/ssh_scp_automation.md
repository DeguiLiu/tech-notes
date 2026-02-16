---
title: "嵌入式开发中的 SSH/SCP 自动化: 从 Expect 脚本到密钥认证的工程实践"
date: 2026-02-16
draft: false
categories: ["blog"]
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

每次编译后需要：
1. 将二进制文件 SCP 到目标板
2. SSH 登录目标板重启服务
3. 拉取日志或 core dump 回宿主机分析

手动输入密码的重复操作在日均数十次的开发迭代中严重影响效率。

## 2. 方案一: Expect 自动化 (快速上手)

[Expect](https://core.tcl-lang.org/expect/index) 是基于 Tcl 的交互自动化工具，通过模式匹配和自动应答实现非交互式操作。

### 2.1 SSH 自动登录

```bash
#!/bin/bash
# ssh_auto.sh -- Expect 自动化 SSH 登录

export TERM=xterm-256color

ip='192.168.1.10'
password='your_password'

# 移除旧主机密钥，避免密钥变更警告
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

### 2.2 SCP 上传文件

```bash
#!/bin/bash
# scp_upload.sh -- Expect 自动化 SCP 上传

export TERM=xterm-256color

file=$1
ip='192.168.1.10'
password='your_password'
dest=${2:-'~/'}

expect -c '
  set timeout 30
  set password "'"$password"'"
  spawn scp -o StrictHostKeyChecking=no '"$file"' root@'"$ip"':'"$dest"'
  expect {
    "*yes/no*" { send "yes\r"; exp_continue }
    "*password:*" { send "$password\r"; exp_continue }
    eof
  }
'
```

### 2.3 SCP 下载文件

```bash
#!/bin/bash
# scp_download.sh -- Expect 自动化 SCP 下载

export TERM=xterm-256color

remote_file=$1
ip='192.168.1.10'
password='your_password'
dest=${2:-'.'}

expect -c '
  set timeout 30
  set password "'"$password"'"
  spawn scp -o StrictHostKeyChecking=no root@'"$ip"':'"$remote_file"' '"$dest"'
  expect {
    "*yes/no*" { send "yes\r"; exp_continue }
    "*password:*" { send "$password\r"; exp_continue }
    eof
  }
'
```

### 2.4 Expect 脚本的引号陷阱

Expect 脚本内嵌在 Bash 中时，引号处理是最容易出错的地方：

```
expect -c '...'        整体用单引号包裹，Bash 不解析内部内容
"'"$password"'"        退出单引号 → 双引号包裹变量 → 重新进入单引号
```

展开过程：

| 写法 | Bash 看到的 | Expect 看到的 |
|------|-----------|-------------|
| `'set p "'"$password"'"'` | `set p "` + `Pa$$w0rd` + `"` | `set p "Pa$$w0rd"` |

如果密码包含 Tcl 特殊字符 (`$`, `[`, `]`, `{`, `}`)，还需要在 Expect 层面转义。这种多层转义是 Expect 方案最大的维护负担。

### 2.5 局限性

| 问题 | 说明 |
|------|------|
| **密码明文** | 密码硬编码在脚本中，任何有读权限的用户都能看到 |
| **进程可见** | `ps aux` 可能显示命令行中的密码 |
| **多层转义** | Bash + Tcl 双层引号处理，密码含特殊字符时极易出错 |
| **无断点续传** | SCP 传输中断后必须重新开始 |
| **绕过主机验证** | `StrictHostKeyChecking=no` 禁用了中间人攻击防护 |

Expect 适合**临时使用**或**密码无法更改的遗留系统**。对于日常开发，应优先考虑密钥认证。

## 3. 方案二: sshpass (轻量替代)

[sshpass](https://sourceforge.net/projects/sshpass/) 是专为 SSH 密码自动化设计的工具，比 Expect 更简洁：

```bash
# 安装
sudo apt install sshpass

# SSH 登录
sshpass -p 'your_password' ssh root@192.168.1.10

# SCP 上传
sshpass -p 'your_password' scp ./app root@192.168.1.10:/opt/

# SCP 下载
sshpass -p 'your_password' scp root@192.168.1.10:/var/log/app.log ./
```

### 3.1 密码传递的安全层级

sshpass 提供三种密码传递方式，安全性递增：

| 方式 | 命令 | 安全性 | 风险 |
|------|------|:------:|------|
| `-p` 命令行 | `sshpass -p 'pwd' ssh ...` | 最低 | `ps` 和 shell history 可见 |
| `-f` 文件 | `sshpass -f /path/to/pwfile ssh ...` | 中等 | 文件权限需设为 `0400` |
| `-e` 环境变量 | `SSHPASS=pwd sshpass -e ssh ...` | 较高 | 用完后应 `unset SSHPASS` |

```bash
# 推荐: 文件方式
echo 'your_password' > ~/.ssh/.board_pwd
chmod 0400 ~/.ssh/.board_pwd

sshpass -f ~/.ssh/.board_pwd ssh root@192.168.1.10
sshpass -f ~/.ssh/.board_pwd scp ./app root@192.168.1.10:/opt/
```

### 3.2 sshpass vs Expect

| 维度 | sshpass | Expect |
|------|:-------:|:------:|
| 安装 | `apt install sshpass` | `apt install expect` |
| SSH/SCP 专用 | 是 | 通用交互自动化 |
| 代码量 | 一行命令 | 10+ 行脚本 |
| 特殊字符处理 | 无需额外转义 | Bash + Tcl 双层转义 |
| 复杂交互 (sudo, 菜单) | 不支持 | 支持 |

**结论**: 仅自动化 SSH/SCP 密码输入时，sshpass 比 Expect 更简洁。需要自动化 sudo 提权、交互式菜单等复杂场景时，Expect 仍有价值。

## 4. 方案三: SSH 密钥认证 (推荐)

SSH 公钥认证是自动化登录的**工业标准方案**。无密码传输、无明文存储、抗暴力破解。

### 4.1 密钥生成与部署

```bash
# 1. 生成 Ed25519 密钥对 (比 RSA 更短、更快、更安全)
ssh-keygen -t ed25519 -C "dev@workstation" -f ~/.ssh/id_ed25519_board
# 输入 passphrase (可选，加密私钥)

# 2. 部署公钥到目标板
ssh-copy-id -i ~/.ssh/id_ed25519_board.pub root@192.168.1.10
# 或手动:
# cat ~/.ssh/id_ed25519_board.pub | ssh root@192.168.1.10 \
#   'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'

# 3. 验证免密登录
ssh -i ~/.ssh/id_ed25519_board root@192.168.1.10
```

### 4.2 权限要求

目标板上的权限必须严格设置，否则 SSH 会拒绝密钥认证：

| 路径 | 权限 | 说明 |
|------|:----:|------|
| `~/.ssh/` | `700` | 仅所有者可读写执行 |
| `~/.ssh/authorized_keys` | `600` | 仅所有者可读写 |
| `~/.ssh/id_ed25519` (私钥) | `600` | 仅所有者可读写 |

```bash
# 目标板上检查
ls -la ~/.ssh/
# drwx------  root root  .ssh/
# -rw-------  root root  authorized_keys
```

### 4.3 ssh-agent 管理密钥

如果私钥设置了 passphrase，每次使用都需要输入。`ssh-agent` 可以在会话期间缓存解密后的私钥：

```bash
# 启动 agent (通常 shell 启动时自动加载)
eval "$(ssh-agent -s)"

# 添加密钥 (输入一次 passphrase)
ssh-add ~/.ssh/id_ed25519_board

# 后续所有 SSH/SCP 操作自动使用缓存的密钥
ssh root@192.168.1.10      # 无需输入任何密码
scp ./app root@192.168.1.10:/opt/
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

当项目涉及多台目标板时，在 `~/.ssh/config` 中配置别名，避免记忆 IP 和参数：

```ssh
# ~/.ssh/config

# === 嵌入式目标板 ===
Host board
    HostName 192.168.1.10
    User root
    IdentityFile ~/.ssh/id_ed25519_board
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new

Host board-zynq
    HostName 192.168.1.20
    User root
    Port 2222
    IdentityFile ~/.ssh/id_ed25519_board

# === 跳板机 (公司内网 → 实验室网络) ===
Host lab-gateway
    HostName 10.0.0.1
    User engineer
    IdentityFile ~/.ssh/id_ed25519_work

# === 通过跳板机访问实验室内的目标板 ===
Host lab-board
    HostName 192.168.100.10
    User root
    ProxyJump lab-gateway
    IdentityFile ~/.ssh/id_ed25519_board

# === 全局默认 ===
Host *
    AddKeysToAgent yes
    IdentitiesOnly yes
    ServerAliveInterval 60
    ServerAliveCountMax 3
    ConnectTimeout 10
```

配置后的使用：

```bash
# 登录目标板 (自动查找 IP、用户名、密钥)
ssh board

# SCP 上传
scp ./app board:/opt/

# 通过跳板机登录实验室目标板 (自动两跳)
ssh lab-board

# 通过跳板机 SCP 文件到实验室目标板
scp ./firmware.bin lab-board:/tmp/
```

### 5.1 关键配置项说明

| 配置项 | 说明 |
|--------|------|
| `IdentitiesOnly yes` | 仅使用指定的密钥文件，不尝试 agent 中的其他密钥 |
| `StrictHostKeyChecking accept-new` | 首次连接自动接受并保存主机密钥，后续连接验证 (比 `no` 更安全) |
| `ServerAliveInterval 60` | 每 60 秒发送心跳，防止 NAT 超时断连 |
| `AddKeysToAgent yes` | 首次使用密钥时自动添加到 ssh-agent |
| `ConnectTimeout 10` | 连接超时 10 秒，避免目标板离线时长时间等待 |

### 5.2 ProxyJump 跳板机

嵌入式开发中常见场景：宿主机在办公网络，目标板在实验室网络，需要通过跳板机 (bastion host) 中转。

```
宿主机 (办公网)  →  跳板机 (双网卡)  →  目标板 (实验室网)
10.0.0.100          10.0.0.1              192.168.100.10
                    192.168.100.1
```

`ProxyJump` (OpenSSH 7.3+) 实现端到端加密的跳板连接，**私钥始终留在宿主机上**，不需要在跳板机上存放任何密钥：

```bash
# 命令行方式
ssh -J engineer@10.0.0.1 root@192.168.100.10

# SCP 通过跳板机
scp -J engineer@10.0.0.1 ./app root@192.168.100.10:/opt/

# 多级跳板
ssh -J bastion1,bastion2 root@target
```

与 `ForwardAgent` 的区别：

| 方式 | 私钥位置 | 安全风险 |
|------|---------|---------|
| `ProxyJump` | 始终在宿主机 | 跳板机无法窃取私钥 |
| `ForwardAgent yes` | 转发到跳板机 | 跳板机 root 用户可劫持 agent |

**推荐使用 `ProxyJump`**，仅在信任跳板机时使用 `ForwardAgent`。

## 6. rsync: 嵌入式开发的增量同步

嵌入式开发中，反复将编译产物推送到目标板是高频操作。`scp` 每次传输完整文件；`rsync` 使用 delta 算法，仅传输变化的部分。

### 6.1 基本用法

```bash
# 同步目录到目标板 (增量传输)
rsync -avz --progress ./build/output/ board:/opt/app/

# 从目标板拉取日志
rsync -avz board:/var/log/app/ ./logs/

# 通过跳板机同步 (利用 SSH Config 的 ProxyJump)
rsync -avz ./build/output/ lab-board:/opt/app/
```

### 6.2 常用选项

| 选项 | 说明 |
|------|------|
| `-a` | 归档模式: 递归、保留权限/时间戳/符号链接 |
| `-v` | 显示传输过程 |
| `-z` | 传输时压缩 (低带宽链路有效) |
| `--progress` | 显示进度 |
| `--delete` | 删除目标端多余的文件 (镜像同步) |
| `--exclude '*.o'` | 排除中间文件 |
| `-e 'ssh -p 2222'` | 指定 SSH 端口 |

### 6.3 SCP vs rsync 性能对比

| 场景 | SCP | rsync |
|------|:---:|:-----:|
| 首次传输 10 MB 二进制 | 1.5 s | 1.8 s (校验开销) |
| 修改 1 KB 后重新传输 | 1.5 s (全量) | **0.3 s** (增量) |
| 传输中断后恢复 | 从头开始 | `--partial` 断点续传 |
| 同步整个目录 | 逐文件 SCP | 批量增量，删除多余文件 |

**结论**: 嵌入式迭代开发中，rsync 的增量传输可以将部署时间缩短 80% 以上。

### 6.4 嵌入式目标板上的 rsync

部分精简的嵌入式 Linux 镜像可能没有预装 rsync。解决方案：

```bash
# 方案 1: 交叉编译静态链接的 rsync
# 方案 2: 使用 Buildroot/Yocto 添加 rsync 包
# 方案 3: 退回 SCP (目标板无需额外软件)
```

## 7. 实用脚本: 一键部署

综合以上技术，构建一个嵌入式开发部署脚本：

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
# rsync 增量同步 (利用 SSH Config 中的别名和密钥)
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

## 9. 安全加固清单

| 措施 | 命令/配置 | 说明 |
|------|----------|------|
| 禁用密码登录 | `/etc/ssh/sshd_config: PasswordAuthentication no` | 目标板仅允许密钥登录 |
| 限制 root 登录 | `PermitRootLogin prohibit-password` | 允许密钥登录 root，禁止密码 |
| 使用 Ed25519 | `ssh-keygen -t ed25519` | 比 RSA-2048 更安全、更快 |
| 主机密钥验证 | `StrictHostKeyChecking accept-new` | 首次连接接受，后续验证 |
| 密钥 passphrase | 生成密钥时设置 | 私钥被盗时仍需 passphrase |
| 文件权限 | `chmod 600 ~/.ssh/config` | 配置文件仅所有者可读 |
| 脚本权限 | `chmod 700 deploy.sh` | 包含敏感路径的脚本限制权限 |

## 10. 总结

| 方案 | 安全性 | 复杂度 | 适用场景 |
|------|:------:|:------:|---------|
| **SSH 密钥 + Config** | 高 | 一次配置 | 日常开发 (推荐) |
| **sshpass -f** | 中 | 低 | 无法部署密钥的遗留设备 |
| **Expect** | 低 | 高 | 复杂交互场景 (sudo/菜单) |
| **rsync** | -- | 低 | 频繁文件同步 (推荐搭配密钥) |
| **ProxyJump** | 高 | 低 | 跳板机场景 (推荐) |

嵌入式开发的推荐工具链：**Ed25519 密钥 + SSH Config + rsync + ProxyJump**。Expect 和 sshpass 作为无法部署密钥时的降级方案。

## 参考资料

1. [Expect 官方文档](https://core.tcl-lang.org/expect/index)
2. [SSH Password Automation with sshpass](https://www.redhat.com/en/blog/ssh-automation-sshpass) (Red Hat)
3. [SSH Public Key Authentication](https://runcloud.io/blog/ssh-public-key-authentication) (RunCloud)
4. [SSH ProxyJump and ProxyCommand Tutorial](https://goteleport.com/blog/ssh-proxyjump-ssh-proxycommand/) (Teleport)
5. [OpenSSH Server Best Security Practices](https://www.cyberciti.biz/tips/linux-unix-bsd-openssh-server-best-practices.html) (nixCraft)
6. [rsync vs SCP: Transfer Speed and Efficiency](https://www.howtouselinux.com/post/what-is-rsync-in-linux-and-is-rsync-faster-than-scp)
7. [SSH Config: The Complete Guide](https://devtoolbox.dedyn.io/blog/ssh-config-complete-guide) (DevToolbox)
8. [Bash Shell 脚本高级编程指南](http://tldp.org/LDP/abs/html/)
