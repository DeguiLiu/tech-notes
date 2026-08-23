# PySide6 工业上位机的实时帧链、零拷贝与跨语言架构

>
> 适合读者：正在用 PySide6 写工业控制、相机或数据采集上位机；想理解"Qt 主线程真的只能有一个，但 GIL 不是性能末日"；被 4 MiB/帧的大块数据复制、OpenGL 上传黑屏、PyInstaller 产物臃肿反复折磨；在做 Python/C++ 混合架构，想搞清楚零拷贝究竟在哪里剪。

主线有三条：**线程与背压、零拷贝与跨语言边界、打包与解释器**。每一节按「原理→证据→方案→代价」组织。

---

## 一、PySide6 的“主线程只有一个”不等于“只能有一个真线程”

PySide6 的事件循环确实只在 GUI 主线程，但 Python 的 `QThread`、`threading.Thread`、C++ 的 `std::thread` 都是真实 OS 线程。GIL 只限制**同一解释器**中**纯 Python 字节码**的并行执行；NumPy、`ctypes.CDLL` 调用（释放 GIL）、C++ worker、OpenGL 驱动、USB SDK 都能在 native 区域并行。

所以正确目标不是"创建更多 QThread"，而是**缩短 Python 持有 GIL 的热路径**，把大计算和大数据移动留在 native 层。

```mermaid
flowchart LR
    A[Qt GUI 主线程<br/>事件/控件/OpenGL]:::gui -->|queued signal<br/>small payload| B(CommandWorker<br/>唯一设备 owner):::worker
    A -->|dirty flag<br/>latest token| C(FramePumpWorker<br/>采集轮询):::worker
    B -->|ctypes 释放 GIL| D[hcs_core.dll<br/>C++ 真线程]:::native
    C -->|FrameChannel<br/>SPSC ring| D
    D -->|token + metadata| A
    D --> E[USB / UVC<br/>SDK]:::device
    classDef gui fill:#1f3a5f,stroke:#7fb3ff,color:#ffffff
    classDef worker fill:#3a5a3a,stroke:#9ccc65,color:#ffffff
    classDef native fill:#5a3a3a,stroke:#ff8a80,color:#ffffff
    classDef device fill:#5a4a1f,stroke:#ffd54f,color:#000000
```

GUI 主线程允许：事件、控件、Qt 状态、OpenGL context、轻量 token 拉取。
GUI 主线程禁止：USB 往返、join/wait、整帧 NumPy reduction、编码、写盘。

---

## 二、线程拓扑：控制面与数据面分离，三类 QoS 队列

把实时数据、可丢数据、可靠数据、控制消息分别给不同 QoS：

```mermaid
flowchart TB
    subgraph ctrl["控制面（小消息/强顺序）"]
        GUI[GUI Qt 主线程]:::gui -->|enqueue HIGH/NORMAL/LOW| CW(CommandWorker<br/>串行 + 优先级 + coalesce):::worker
        CW -->|ctypes 释放 GIL| ABI[hcs_core Control/AsyncBus]:::native
        ABI --> CMD[设备命令通道]:::device
    end
    subgraph data["数据面（大帧/低延迟）"]
        USB[USB 30 fps]:::device --> CAP[C++ CaptureThread<br/>高优先级]:::native
        CAP --> PAR[C++ Parse/Normalize<br/>Y16 + 信息行]:::native
        PAR --> FC{FrameChannel<br/>token + generation}:::native
    end
    FC -->|latest-wins| DISP[DisplayPump]:::worker
    FC -->|coalescing| ANA[AnalyticsPool<br/>温度/直方图]:::worker
    FC -->|reliable ring| REC[RecorderQueue]:::worker
    GUI -. dirty flag .-> FC
    GUI -. latest token .-> DISP
    classDef gui fill:#1f3a5f,stroke:#7fb3ff,color:#ffffff
    classDef worker fill:#3a5a3a,stroke:#9ccc65,color:#ffffff
    classDef native fill:#5a3a3a,stroke:#ff8a80,color:#ffffff
    classDef device fill:#5a4a1f,stroke:#ffd54f,color:#000000
```

```mermaid
flowchart LR
    Q1[Capture SPSC<br/>可靠有界]:::reliable -->|记 overrun<br/>不静默覆盖| F1[Fault]:::fault
    Q2[Display latest slot<br/>最新帧胜出]:::latest -->|覆盖旧 token<br/>记 display_dropped| OK1[继续]:::ok
    Q3[Recorder 可靠队列]:::reliable -->|满载停止告警| WARN[显式停止]:::warn
    Q4[Analytics coalescing<br/>按 task key 合并]:::coalesce -->|结果带 frame_id| OK2[继续]:::ok
    Q5[Command 优先级<br/>HIGH/NORMAL/LOW]:::priority -->|满载合并/拒绝| OK3[继续]:::ok
    classDef reliable fill:#1f4f7a,stroke:#4fc3f7,color:#ffffff
    classDef latest fill:#7a4f1f,stroke:#ffb74d,color:#ffffff
    classDef coalesce fill:#4f7a1f,stroke:#aed581,color:#ffffff
    classDef priority fill:#7a1f4f,stroke:#f48fb1,color:#ffffff
    classDef fault fill:#7a1f1f,stroke:#ef5350,color:#ffffff
    classDef warn fill:#7a5a1f,stroke:#ffd54f,color:#000000
    classDef ok fill:#1f4f1f,stroke:#66bb6a,color:#ffffff
```

要点：

- **采集线程（C++）**只做"拿一帧、盖章、发布"。不调 Python、不打日志（异步）、不进 Python GIL。
- **Python FramePump**：`QObject` 放到长驻 `QThread`，每 tick 只发 latest descriptor + dirty flag。**不发 `Signal(object)` 携带每帧 ndarray**，否则 Qt posted-event 队列会积压。
- **CommandWorker**：唯一设备 owner，串行执行 `get/set/exec`，按 HIGH/NORMAL/LOW 优先级，滑块按 `(panel, key)` coalesce；GUI 只 enqueue，绝不直接同步调真实设备。
- **PySide6 的 `QThread` 易踩点**：`QThread` 对象本身仍属于创建它的线程（一般是 GUI），只有 `worker.moveToThread(thread)` 之后 worker 才在新线程里跑。

读者读到这里自然会有两个问题：上面这个 `FrameChannel` 上的"零拷贝"到底是什么，4 MiB 帧到底怎么走才不被反复复制？这是接下来 §三 的主题。

---

## 三、零拷贝的真实定义：哪里可以证明生命周期

“零拷贝”不是“哪里都不复制”，而是**大帧在可证明的 slot lease（代数、引用计数、显式 release）下只传递视图**；写盘、编码、跨生命周期或无法证明所有权时允许一次稳定复制。

```mermaid
sequenceDiagram
    participant SDK as USB SDK
    participant CAP as C++ CaptureThread
    participant FP as FramePool slot
    participant PY as Python consumer
    participant GL as OpenGL 上传
    Note over SDK,CAP: 不调 Python、不打日志、不做 reduction
    SDK->>CAP: 借用指针 + frame_bytes
    CAP->>FP: acquire slot
    CAP->>FP: write payload
    CAP->>FP: state=READY (release)
    CAP->>PY: publish token (frame_id, generation)
    PY->>FP: acquire(token)
    FP-->>PY: numpy view (无 copy)
    PY->>GL: glTexSubImage2D(view)
    PY->>FP: release(token)
    Note over FP,GL: stop 顺序：停 producer → drain consumer → close
```

```mermaid
flowchart TD
    A[callback string_at]:::copy -->|+1 copy| B(UvcCameraBackend.read):::copy
    B -->|+1 copy| C(FrameWorker.snapshot):::copy
    C -->|+1 copy 录制时| D(Recorder.write_frame):::copy
    D -->|+1 copy 编码时| E(PNG/写盘 staging):::copy
    E --> F([整帧复制账本<br/>单帧可达 5 次]):::warn
    A -. 允许 .-> OK[一次性安全回退]:::ok
    B -. 允许 .-> OK
    E -. 允许 .-> OK
    C -. 禁止 .-> NG[同一 payload<br/>重复复制]:::ng
    D -. 禁止 .-> NG
    classDef copy fill:#5a3a3a,stroke:#ff8a80,color:#ffffff
    classDef warn fill:#7a5a1f,stroke:#ffd54f,color:#000000
    classDef ok fill:#1f4f1f,stroke:#66bb6a,color:#ffffff
    classDef ng fill:#7a1f1f,stroke:#ef5350,color:#ffffff
```

**允许的复制**：C ABI callback 指针没有稳定生命周期时的安全回退；写盘/PNG/编码必须拥有独立 buffer 时的一次复制；小型 metadata 转为 `bytes`；用户主动请求的快照。

**禁止的复制**：同一 payload 在 callback/GUI slot/recorder 入队/display publish 上重复复制；为了“线程安全”给 SHM view 加 Python `Lock`；把 4 MiB ndarray pickle 进 `multiprocessing.Queue` 或 Qt queued signal；在 GUI 线程做状态栏 reduction。

我们用的 token + 引用计数契约：

```cpp
struct FrameToken {
    uint64_t frame_id;
    uint64_t generation;   // 防止 ring wrap 后误读
    uint32_t slot_index;
    uint32_t payload_bytes;
    uint32_t width, height, format;
    uint64_t capture_ns;
};

struct FrameSlotHeader {
    std::atomic<uint32_t> state;     // FREE / WRITING / READY
    std::atomic<uint32_t> ref_count;
    uint64_t generation, frame_id;
    uint32_t payload_bytes, metadata_bytes;
};
```

Python 侧只能调 `acquire(token) / view(token) / release(token)`，**不能直接改 `state`/`ref_count`**。如果不引入引用计数，至少给 display 和 recorder 各预留独立的 staging 双/三缓冲，并明确降级、把 `copy_bytes` 计入指标。

录制端再省一刀：

```python
raw_file.write(memoryview(frame).cast("B"))   # 0.005 ms vs tobytes 1.73 ms
```

约束：frame 必须是连续、小端、生命周期稳定；非连续或字节序不匹配时允许显式复制。

---

## 四、复合帧必须共享 frame_id

工业相机常传复合帧：1920×1080 图像 + 2 行原始信息行（带温度、状态、checksum）。最常见的 bug 是“GUI 拉一帧、统计延迟两秒、结果贴错帧”。

```mermaid
flowchart LR
    RAW[raw payload<br/>1920 × 1082 × 2]:::raw --> CHK{校验<br/>payload_bytes<br/>stride<br/>endianness}:::check
    CHK -->|OK| IMG[image = rows 0:1080]:::img
    CHK -->|OK| INF[info = rows 1080:1082<br/>保留原始 bytes]:::meta
    INF --> PAR[versioned parser]:::parse
    PAR --> T[temperature<br/>frame_id=X]:::temp
    IMG --> D[display<br/>frame_id=X]:::disp
    T -. 旧 frame_id 拒绝 .-> N([不覆盖新帧]):::ng
    CHK -->|FAIL| FLAG[标记 metadata_error<br/>图像仍可显示]:::warn
    classDef raw fill:#3a3a5a,stroke:#9fa8da,color:#ffffff
    classDef check fill:#5a4a1f,stroke:#ffd54f,color:#000000
    classDef img fill:#1f4f7a,stroke:#4fc3f7,color:#ffffff
    classDef meta fill:#4f7a1f,stroke:#aed581,color:#ffffff
    classDef parse fill:#7a4f1f,stroke:#ffb74d,color:#ffffff
    classDef temp fill:#7a1f4f,stroke:#f48fb1,color:#ffffff
    classDef disp fill:#1f3a5f,stroke:#7fb3ff,color:#ffffff
    classDef ng fill:#7a1f1f,stroke:#ef5350,color:#ffffff
    classDef warn fill:#5a4a1f,stroke:#ffd54f,color:#000000
```

**图像和 metadata 必须用同一个 `frame_id` 对齐**；旧结果不能覆盖新帧；解析失败只标记 `metadata_error`，图像仍可显示——绝不能“猜温度”。

这一条直接服务于 §三 的零拷贝契约：只有当 slot 把 image / infoline / metadata 放在同一 token 上、消费者按 `frame_id` 校验代际时，跨语义的“同一帧”才有保证。

---

## 五、OpenGL 上传：Intel 驱动黑屏与 PySide6 PBO 绑定坑

帧数据最终要落到屏幕。工业上位机常跑老 Intel 集显。`GL_R16` / `GL_R16UI` 是 16-bit 单通道纹理最干净的接口，但**实测在多个 Intel 驱动下上传后屏幕全黑**。

```mermaid
flowchart LR
    Y16[Y16 frame<br/>uint16]:::data --> PICK{启动时探测<br/>GL version<br/>RG8 + shader<br/>驱动白名单}:::probe
    PICK -->|RG8 OK| RG8[GL_RG8 + UNSIGNED_BYTE<br/>low→R high→G]:::ok
    RG8 --> SH[fragment shader<br/>重建 16-bit<br/>查伪彩 LUT]:::shader
    SH --> OUT[screen]:::out
    PICK -->|GL_R16 OK| R16[GL_R16 / GL_R16UI<br/>Intel 黑屏 ⚠]:::ng
    PICK -->|都不行| CPU[CPU fallback<br/>CPU LUT→RGBA staging]:::warn
    CPU --> OUT
    classDef data fill:#3a3a5a,stroke:#9fa8da,color:#ffffff
    classDef probe fill:#5a4a1f,stroke:#ffd54f,color:#000000
    classDef ok fill:#1f4f1f,stroke:#66bb6a,color:#ffffff
    classDef shader fill:#1f4f7a,stroke:#4fc3f7,color:#ffffff
    classDef out fill:#1f3a5f,stroke:#7fb3ff,color:#ffffff
    classDef ng fill:#7a1f1f,stroke:#ef5350,color:#ffffff
    classDef warn fill:#7a5a1f,stroke:#ffb74d,color:#000000
```

我们改用 `GL_RG8 + GL_UNSIGNED_BYTE` 双通道：把 little-endian Y16 的低/高字节分别送 R/G，fragment shader 内重建 16-bit 值再查伪彩 LUT。这条路在 Intel/NVIDIA/AMD 上都能跑。

PBO（pixel buffer object）异步上传在 OpenGL 教科书里几乎是“必须”，但在 PySide6 6.11.1 的 `glTexImage2D/glTexSubImage2D` 绑定上有真实约束：

- 绑定的 `pixels` 参数只接收 buffer 对象；
- `None`、`0`、`ctypes.c_void_p(0)` 分别被拒绝或直接崩溃；
- 不是配置问题，是绑定层根本不支持“null/整数偏移”惯用法。

```python
# 不再是可选项：首次使用即永久回落直传
def _disable_pbo_upload(self):
    self._pbo_enabled = False   # glTexSubImage2D 直传是既定出货路径
```

CPU fallback 不是性能验收路径：必须单独记录帧率和 P95/P99，**不能用它证明硬件 GPU 路径的预算达标**。

---

## 六、NumPy LUT 与归一化的工程经验

GPU 拿到的是归一化后的 uint8 纹理。归一化本身是热路径，必须先在 NumPy 内优化再下沉 native：

```python
# 65536 项 LUT（约 64 KiB），只在下限/上限变化时重建
codes = np.arange(65536, dtype=np.float32)
lut = np.clip((codes - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)

# 预分配 gray8，禁止每帧创建
np.take(lut, frame_y16, out=gray8)
```

```mermaid
flowchart LR
    A[Y16 frame<br/>1920×1080 uint16]:::data --> B{LUT 缓存命中<br/>且窗口未变?}:::check
    B -->|是| C[预分配 gray8]:::buf
    B -->|否| D[重建 64 KiB LUT<br/>一次 float32]:::lut
    D --> C
    C --> E[np.take LUT<br/>out=gray8]:::work
    E --> F[显示 / 编码]:::out
    A --> S[(统计<br/>1.5 s 节拍<br/>保持原始 Y16 计数)]:::stats
    classDef data fill:#3a3a5a,stroke:#9fa8da,color:#ffffff
    classDef check fill:#5a4a1f,stroke:#ffd54f,color:#000000
    classDef buf fill:#4f7a1f,stroke:#aed581,color:#ffffff
    classDef lut fill:#7a4f1f,stroke:#ffb74d,color:#ffffff
    classDef work fill:#1f4f7a,stroke:#4fc3f7,color:#ffffff
    classDef out fill:#1f3a5f,stroke:#7fb3ff,color:#ffffff
    classDef stats fill:#7a1f4f,stroke:#f48fb1,color:#ffffff
```

**几条经验**：

1. LUT 只在窗口变化时重建，**禁止每帧重建**；
2. `gray8` 用固定预分配，禁止每帧 `np.empty/zeros/array`；
3. raw Y16 录制**完全绕过归一化**，保留原始计数用于测温换算；
4. 统计和归一化**必须独立输出**，禁止两个线程写同一 ndarray 区间；
5. `Y16 >> 8` 虽然便宜，但丢动态范围、破坏低对比度显示，且**后续温度换算直接算错**——不允许作为优化手段；
6. 测温状态使用原始 Y16 计数的 min/max/mean，不能用右移后的 uint8 代入。

并行归一化：先测单 worker 的 `np.take(out=...)` P95；只有仍超预算才用 `ThreadPoolExecutor(max_workers=2)` 按行切成互不重叠区间。因为 NumPy native 操作通常释放 GIL，能拿到真并行；但**若双 worker 没有降低 P95 就恢复单 worker**——不能凭“多线程一定更快”下结论。

---

## 七、我们验证下来最稳的默认架构

把前面六节的所有设计拼起来，就是这张默认架构图：

```mermaid
flowchart TB
    subgraph MAIN["唯一可见窗口"]
        UI[hcs_client.exe<br/>PySide6 GUI]:::gui
        FWP[FramePumpWorker<br/>moveToThread]:::worker
        CW[CommandWorker<br/>唯一 owner]:::worker
        RW[Recorder worker<br/>可靠 raw]:::worker
    end
    subgraph CORE["hcs_core.dll 同进程 C++ 真线程"]
        CAP[CaptureThread<br/>USB 拉流]:::native
        PAR[Parse/Normalize<br/>Y16 + 信息行]:::native
        ANA[AnalyticsPool<br/>直方图/温度/LUT]:::native
    end
    subgraph OPT["可选"]
        CPU[无窗口 CPU worker<br/>仅 GIL 重任务]:::opt
    end
    UI <-->|dirty + token| FWP
    UI <-->|enqueue HIGH/NORM/LOW| CW
    UI <-->|record start/stop| RW
    FWP <-->|FrameChannel| CAP
    CAP --> PAR
    PAR --> ANA
    CW <-->|ctypes 释放 GIL| CORE
    CORE -. 按需 SHM .-> CPU
    classDef gui fill:#1f3a5f,stroke:#7fb3ff,color:#ffffff
    classDef worker fill:#3a5a3a,stroke:#9ccc65,color:#ffffff
    classDef native fill:#5a3a3a,stroke:#ff8a80,color:#ffffff
    classDef opt fill:#3a3a5a,stroke:#9fa8da,color:#ffffff
```

各节点的设计来源：

- **GUI ↔ FramePump** 对应 §二的 dirty/token 流；
- **FrameChannel** 是 §三 的零拷贝总线，所有大帧都走 token；
- **CaptureThread + ParseThread** 共同保证 §四 的 `frame_id` 对齐；
- **AnalyticsPool** 与 §六 的归一化紧密耦合，归一化结果直接喂给 OpenGL；
- **CommandWorker** + `ctypes` 释放 GIL 是 §一 的具体落地；
- **可选 CPU worker** 的开关标准是 §七 末尾的"代价-收益"。

**一个窗口、同进程 C++ 真线程、至多一个按需 CPU worker**——这是我们验证下来性能、故障隔离和工程复杂度三者最均衡的位置。

---

## 八、跨语言硬约束 E1–E5

整套设计能否成立，取决于几条跨语言硬约束不被破坏。下图把每一类事故对应到原始规则：

```mermaid
flowchart TD
    A[NumPy 视图]:::py --> B{C++ 段<br/>仍存活?}:::check
    B -->|是| OK[安全使用]:::ok
    B -->|否| UAF[悬垂访问<br/>UAF / 撕裂]:::ng
    C[Python Lock]:::py --> D[保护 C++ SHM]:::ng
    D --> E[锁语义错配<br/>跨进程/跨语言不成立]:::ng
    F[耗时 ctypes 调用<br/>未释放 GIL]:::py --> G[GUI 线程卡顿]:::ng
    H[重复 close / 半初始化<br/>不幂等清理]:::ng2 --> I[句柄/SHM/view 残留]:::ng
    classDef py fill:#1f3a5f,stroke:#7fb3ff,color:#ffffff
    classDef check fill:#5a4a1f,stroke:#ffd54f,color:#000000
    classDef ok fill:#1f4f1f,stroke:#66bb6a,color:#ffffff
    classDef ng fill:#7a1f1f,stroke:#ef5350,color:#ffffff
    classDef ng2 fill:#7a1f1f,stroke:#ef5350,color:#ffffff
```

- **E1**：NumPy 视图生命周期不得超过 C++ 共享段 + slot lease。
- **E2**：ctypes 结构体、宽度、packing、常量集中定义并有 ABI 测试。
- **E3**：C++ 内存只走 C ABI 原子原语 / FrameChannel 头尾，**Python Lock 不能保护 C++ SHM**。
- **E4**：昂贵 native 调用必须释放 GIL，且**不能从 Qt GUI 线程调用**。
- **E5**：中断、半初始化、拔插、重复 close 都是正常输入；清理必须幂等。
- **设备 handle 单 owner**：一个设备 handle 只能有一个串行 owner；`hcs_stop`、callback drain、`hcs_close` 必须按顺序完成。

E1/E3 是 §三 零拷贝契约的底线；E4 是 §一 / §二 线程拓扑能够成立的前提；E5 是 §七 默认架构在拔插、崩溃下仍然稳定的根因。任何一条被破坏，前面的所有优化都会立刻失效。

---

如果你也在做类似的工业 PySide6 项目，欢迎交流哪些经验对你有用。
