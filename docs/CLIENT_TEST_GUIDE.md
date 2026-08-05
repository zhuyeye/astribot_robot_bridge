# Astribot Robot Bridge — 客户端测试与开发指南

面向**开发机 / 上位机**联调：不需要安装 Astribot SDK，只需能访问 Orin 上的 Bridge HTTP 端口。

**完整 API 接入说明（端点索引、协议、WebSocket 格式、业务流）：** [API_REFERENCE.md](API_REFERENCE.md)

| 项 | 默认值 |
|----|--------|
| Orin Bridge 地址 | `http://<ORIN_IP>:8080` |
| OpenAPI 交互文档 | `http://<ORIN_IP>:8080/docs` |
| WebSocket 状态流 | `ws://<ORIN_IP>:8080/v1/ws/state` |
| WebSocket 实时控制 | `ws://<ORIN_IP>:8080/v1/ws/realtime` |
| WebSocket 音频流 | `ws://<ORIN_IP>:8080/v1/ws/audio` |

下文用环境变量占位：

```bash
export BRIDGE=http://192.168.0.10:8080   # 按实际 Orin IP 修改
# 若配置了 api_key：
# export BRIDGE_API_KEY=your-key
```

---

## 1. 前置条件

### 1.1 Orin 侧

```bash
cd /home/astribot/workspace/astribot_robot_bridge
./scripts/start_bridge.sh
```

默认日志：

- 终端实时输出
- 文件 `logs/bridge.log`

标准化运维脚本：

```bash
./scripts/stop_bridge.sh
./scripts/restart_bridge.sh
```

确认：

```bash
curl -s "$BRIDGE/health"
# {"ok":true,"data":{"status":"ok"},"error":null}

curl -s "$BRIDGE/ready"
# data.ready == true
```

### 1.2 开发机侧依赖

```bash
python3 -m pip install httpx websockets
```

可选：把仓库里的客户端拷到本机：

```text
astribot_robot_bridge/clients/python/bridge_client.py
```

### 1.3 统一响应格式

成功：

```json
{ "ok": true, "data": { ... }, "error": null }
```

失败（HTTP 4xx/5xx，body 仍是 JSON）：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "control_busy",
    "message": "control held by trajectory:wave",
    "details": { "holder": "trajectory:wave" }
  }
}
```

常见 `error.code`：

| code | HTTP | 含义 |
|------|------|------|
| `control_busy` | 409 | 其它运动模式占用控制权；可加 `force: true` |
| `unknown_action` | 404 | 动作 id 不存在 |
| `wrong_mode` / `realtime_inactive` | 409 | 实时会话未开或模式不对 |
| `unauthorized` | 401 | API Key 错误 |
| `audio_busy` | 409 | 音频会话占用 |

额外说明：

- 每个 HTTP 响应都会带 `X-Request-Id`
- 你可以在报错或超时时记录这个 id，再去 Orin 的 `logs/bridge.log` 里全文检索
- 控制类接口会额外返回 `resource`、`execution`、`operation_status`
- 返回 HTTP `202` 时，表示“服务端已经接收并启动后台执行”，**不代表动作已经完成**

典型异步返回示例：

```json
{
  "ok": true,
  "data": {
    "accepted": true,
    "task_id": "2a8c...",
    "status": "pending",
    "resource": "motion.move_to.home",
    "execution": "async",
    "operation_status": "accepted"
  },
  "error": null
}
```

### 1.4 控制权规则（测试前必读）

同一时间只能有一种运动模式：

`idle` → `trajectory` / `move_to` / `realtime`

- 冲突且未 `force` → 409
- `POST /v1/motion/estop` 可打断任意模式
- 音频**不占用**运动控制权，但 speaker 会话自身互斥

Bridge 默认采用“**托管控制权**”策略：

- 正常情况下长期持有控制权
- 每个写控制请求前都会检查控制权
- 如果发现主控制页面偶发接管，bridge 会默认自动尝试恢复一次
- 为避免频繁切换，恢复过程受 `control_rights.reacquire_cooldown_s` 和 `max_reacquire_attempts_per_loss` 限制

---

## 2. 推荐联调顺序（Checklist）

按下面顺序测，可快速判断 Bridge / 网络 / 机器人是否正常：

| # | 步骤 | 命令或接口 | 期望 |
|---|------|------------|------|
| 1 | 连通 | `GET /health` | `ok` |
| 2 | 就绪 | `GET /ready` | `ready=true` |
| 3 | 本体信息 | `GET /v1/robot` | 有 parts / dofs |
| 4 | 读关节 | `GET /v1/joints` | 25 维 pos/vel |
| 5 | 状态流 | `WS /v1/ws/state` | seq 递增 |
| 6 | 列动作 | `GET /v1/actions/` | 非空列表 |
| 7 | 播放动作 | `POST /v1/actions/play` idle | accepted |
| 8 | MoveTo | `POST .../move_to/home` wait | 回 home |
| 9 | 实时 | session → command → close | accepted |
| 10 | 急停 | `POST /v1/motion/estop` | 仅异常时测 |
| 11 | 音频 | `POST /v1/audio/play` | 有喇叭时测 |

---

## 3. curl 速查

以下命令可直接复制。若启用了 API Key，每条加：

```bash
-H "X-API-Key: $BRIDGE_API_KEY"
```

### 3.1 系统

```bash
curl -s "$BRIDGE/health" | python3 -m json.tool
curl -s "$BRIDGE/ready" | python3 -m json.tool
curl -s "$BRIDGE/v1/status" | python3 -m json.tool
curl -s "$BRIDGE/v1/control-rights" | python3 -m json.tool

# 查看响应头里的 X-Request-Id
curl -i "$BRIDGE/health"

# 主控制页面接管后，显式重抢控制权
curl -s -X POST "$BRIDGE/v1/control-rights/reacquire" | python3 -m json.tool
```

### 3.2 信息查询

```bash
# 机器人元信息
curl -s "$BRIDGE/v1/robot" | python3 -m json.tool

# 当前关节（默认 pos+vel，25 DOF readable）
curl -s "$BRIDGE/v1/joints?which=current&fields=pos,vel" | python3 -m json.tool

# 只要头
curl -s "$BRIDGE/v1/joints?names=astribot_head&fields=pos" | python3 -m json.tool

# 期望关节 / 限位 / 笛卡尔
curl -s "$BRIDGE/v1/joints?which=desired&fields=pos" | python3 -m json.tool
curl -s "$BRIDGE/v1/joints/limits" | python3 -m json.tool
curl -s "$BRIDGE/v1/cartesian?which=current&frame=chassis" | python3 -m json.tool

# 相机元数据（不出图）
curl -s "$BRIDGE/v1/cameras" | python3 -m json.tool

# FK / IK
curl -s -X POST "$BRIDGE/v1/kinematics/fk" \
  -H 'Content-Type: application/json' \
  -d '{"names":["astribot_arm_left"],"joints":[[0,0,0,0,0,0,0]]}' | python3 -m json.tool
```

### 3.3 预存动作

```bash
curl -s "$BRIDGE/v1/actions/" | python3 -m json.tool
curl -s "$BRIDGE/v1/actions/idle" | python3 -m json.tool
curl -s "$BRIDGE/v1/actions/status" | python3 -m json.tool

curl -s -X POST "$BRIDGE/v1/actions/play" \
  -H 'Content-Type: application/json' \
  -d '{"action_id":"wave","request_id":"test-1","force":false}' | python3 -m json.tool

# 若想观察 202 + X-Request-Id，用 -i
curl -i -X POST "$BRIDGE/v1/actions/play" \
  -H 'Content-Type: application/json' \
  -d '{"action_id":"wave","request_id":"test-1","force":false}'

# 同动作重复 play 可能 accepted=false（非错误）
curl -s -X POST "$BRIDGE/v1/actions/stop" | python3 -m json.tool
```

常用 `action_id`（以 Orin manifest 为准，`list` 为准）：

`idle`, `wave`, `hello`, `speak`, `confuse`, `apology`, `direct_left`, `direct_right`, `hands_on_hips`, `demo`, …

### 3.4 MoveTo

```bash
# 异步：返回 202 + task_id
curl -s -X POST "$BRIDGE/v1/motion/move_to/home" \
  -H 'Content-Type: application/json' \
  -d '{"force":false,"wait":false}' | python3 -m json.tool

# 查看异步语义和响应头
curl -i -X POST "$BRIDGE/v1/motion/move_to/home" \
  -H 'Content-Type: application/json' \
  -d '{"force":false,"wait":false}'

# 查任务
# curl -s "$BRIDGE/v1/motion/tasks/<task_id>" | python3 -m json.tool

# 同步等待完成（超时要加大）
curl -s -X POST "$BRIDGE/v1/motion/move_to/home" \
  -H 'Content-Type: application/json' \
  -d '{"wait":true,"force":true}' | python3 -m json.tool

# 关节目标（只动头示例）
curl -s -X POST "$BRIDGE/v1/motion/move_to/joints" \
  -H 'Content-Type: application/json' \
  -d '{
    "targets": {"astribot_head": [0.0, 0.0]},
    "duration": 2.0,
    "wait": true,
    "force": true
  }' | python3 -m json.tool
```

### 3.5 实时控制（低频 REST 调试）

```bash
curl -s -X POST "$BRIDGE/v1/motion/realtime/session" \
  -H 'Content-Type: application/json' \
  -d '{"rate_hz":50,"space":"joints","force":true}' | python3 -m json.tool

curl -s -X POST "$BRIDGE/v1/motion/realtime/command" \
  -H 'Content-Type: application/json' \
  -d '{"targets":{"astribot_head":[0.0,0.0]}}' | python3 -m json.tool

curl -s -X POST "$BRIDGE/v1/motion/realtime/close" | python3 -m json.tool
```

> 50Hz+ 闭环请用 WebSocket（见第 5 节），不要用 REST 逐帧。

### 3.6 夹爪 / 设置 / 急停

```bash
curl -s -X POST "$BRIDGE/v1/motion/gripper/open" \
  -H 'Content-Type: application/json' -d '{"duration":1.0}' | python3 -m json.tool

curl -s -X POST "$BRIDGE/v1/motion/gripper/close" \
  -H 'Content-Type: application/json' -d '{"duration":1.0}' | python3 -m json.tool

curl -s -X POST "$BRIDGE/v1/motion/settings" \
  -H 'Content-Type: application/json' \
  -d '{"head_follow":false,"collision_avoidance":true}' | python3 -m json.tool

# 急停：仅人工异常场景
curl -s -X POST "$BRIDGE/v1/motion/estop" | python3 -m json.tool
```

### 3.7 音频

```bash
curl -s "$BRIDGE/v1/audio/clips" | python3 -m json.tool
curl -s "$BRIDGE/v1/audio/status" | python3 -m json.tool
curl -s "$BRIDGE/v1/audio/system-volume" | python3 -m json.tool

curl -s -X POST "$BRIDGE/v1/audio/play" \
  -H 'Content-Type: application/json' \
  -d '{"clip_id":"hello","force":true}' | python3 -m json.tool

# 设置系统播放音量，优先 pactl，失败则回退 amixer
curl -s -X POST "$BRIDGE/v1/audio/system-volume" \
  -H 'Content-Type: application/json' \
  -d '{"volume_percent":60,"unmute":true}' | python3 -m json.tool

curl -s -X POST "$BRIDGE/v1/audio/stop" | python3 -m json.tool
```

`GET /v1/audio/system-volume` 返回示例：

```json
{
  "ok": true,
  "data": {
    "available": true,
    "backend": "pactl",
    "sink": "@DEFAULT_SINK@",
    "volume_percent": 60,
    "muted": false
  },
  "error": null
}
```

如果当前 Orin 没有活动的 `PulseAudio/PipeWire` 会话，Bridge 会自动尝试 `amixer`。两者都失败时返回 `503 system_volume_unavailable`。

`GET /v1/audio/status` 现在还会返回：

- `queue_size` / `queue_capacity`
- `worker_alive`
- `worker_error`
- `last_frame_ts` / `last_publish_ts`
- `dropped_frames`

如果你怀疑“发过音频后 bridge 卡住”，先看这里，再去查对应 `X-Request-Id` 的日志。

---

## 4. Python 客户端（推荐）

### 4.1 CLI

```bash
# 把 bridge_client.py 放到当前目录或 PYTHONPATH
python3 bridge_client.py --base-url "$BRIDGE" health
python3 bridge_client.py --base-url "$BRIDGE" status
python3 bridge_client.py --base-url "$BRIDGE" control-rights
python3 bridge_client.py --base-url "$BRIDGE" reacquire-control-rights
python3 bridge_client.py --base-url "$BRIDGE" robot
python3 bridge_client.py --base-url "$BRIDGE" joints
python3 bridge_client.py --base-url "$BRIDGE" list-actions
python3 bridge_client.py --base-url "$BRIDGE" play idle
python3 bridge_client.py --base-url "$BRIDGE" play wave --force
python3 bridge_client.py --base-url "$BRIDGE" stop-action
python3 bridge_client.py --base-url "$BRIDGE" home --wait
python3 bridge_client.py --base-url "$BRIDGE" audio-play --clip-id hello
python3 bridge_client.py --base-url "$BRIDGE" audio-volume
python3 bridge_client.py --base-url "$BRIDGE" set-audio-volume 60
# python3 bridge_client.py --base-url "$BRIDGE" estop
```

### 4.2 最小集成代码

```python
from bridge_client import BridgeClient

BRIDGE = "http://192.168.0.10:8080"

with BridgeClient(BRIDGE, timeout=60.0) as c:
    assert c.health()["status"] == "ok"
    print("robot:", c.robot()["alive"], c.robot()["readable_dof"])
    print("q head:", c.joints(names="astribot_head", fields="pos"))

    # 社会动作：tag -> action_id 映射放在客户端
    TAG_TO_ACTION = {
        "idle": "idle",
        "wave": "wave",
        "speak": "speak",
        "guide_left": "direct_left",
        "guide_right": "direct_right",
    }

    def on_tag(tag: str) -> None:
        action_id = TAG_TO_ACTION.get(tag)
        if not action_id:
            return
        reply = c.play(action_id, request_id=tag, force=False)
        # accepted=False 表示同动作已在播，通常可忽略
        print(tag, reply)

    on_tag("wave")
```

### 4.3 MoveTo + 任务轮询

```python
import time
from bridge_client import BridgeClient

with BridgeClient("http://192.168.0.10:8080", timeout=120.0) as c:
    # 同步
    print(c.move_to_home(wait=True, force=True))

    # 异步
    reply = c.move_to_joints(
        {"astribot_head": [0.0, 0.0]},
        duration=2.0,
        wait=False,
        force=True,
    )
    task_id = reply["task_id"]
    while True:
        task = c._unwrap(c._client.get(f"/v1/motion/tasks/{task_id}"))
        print(task["status"], task.get("message"))
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.2)
```

### 4.4 自建最小 HTTP 封装（不依赖仓库客户端）

```python
import httpx

class MiniBridge:
    def __init__(self, base: str, api_key: str | None = None):
        h = {"X-API-Key": api_key} if api_key else {}
        self.c = httpx.Client(base_url=base, headers=h, timeout=30.0)

    def call(self, method: str, path: str, **kwargs):
        r = self.c.request(method, path, **kwargs)
        r.raise_for_status()
        body = r.json()
        if not body.get("ok", True):
            raise RuntimeError(body.get("error"))
        return body["data"]
```

---

## 5. WebSocket 联调

### 5.1 状态流 ` /v1/ws/state`

查询参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `hz` | 50 | 1–100 |
| `fields` | `joints.pos,joints.vel` | 可用 `joints.pos` / `joints.vel` / `cartesian` |

推送示例字段：

```json
{
  "seq": 12,
  "t": 1719300000.12,
  "mode": "idle",
  "joints": {
    "parts": ["astribot_chassis", "..."],
    "joint_names": ["..."],
    "pos": { "astribot_head": [0.0, 0.0], "...": "..." },
    "pos_flat": [ "... 25 floats ..." ],
    "vel": { },
    "vel_flat": [ ]
  }
}
```

完整脚本：

```python
import asyncio
import json
import websockets

BRIDGE_HOST = "192.168.0.10"
URI = f"ws://{BRIDGE_HOST}:8080/v1/ws/state?hz=50&fields=joints.pos,joints.vel"

async def main():
    async with websockets.connect(URI, max_size=8_000_000) as ws:
        for i in range(100):
            msg = json.loads(await ws.recv())
            flat = msg.get("joints", {}).get("pos_flat")
            print(f"seq={msg['seq']} mode={msg['mode']} dof={len(flat) if flat else None}")
            if i == 0:
                print("parts:", msg["joints"]["parts"])

asyncio.run(main())
```

录制 5 秒到文件：

```python
import asyncio, json, time, websockets

async def record(path="states.jsonl", seconds=5):
    uri = "ws://192.168.0.10:8080/v1/ws/state?hz=50&fields=joints.pos"
    t_end = time.time() + seconds
    async with websockets.connect(uri) as ws, open(path, "w") as f:
        while time.time() < t_end:
            f.write(await ws.recv() + "\n")
    print("wrote", path)

asyncio.run(record())
```

### 5.2 实时控制 ` /v1/ws/realtime`

建议流程：先 REST 开会话（或 WS 发 `open`）→ WS 推帧 → `close`。

```python
import asyncio
import json
import httpx
import websockets

BASE = "http://192.168.0.10:8080"
WS = "ws://192.168.0.10:8080/v1/ws/realtime"

async def main():
    # 抢占控制权并开会话
    with httpx.Client(base_url=BASE, timeout=10) as c:
        r = c.post("/v1/motion/realtime/session", json={"rate_hz": 50, "space": "joints", "force": True})
        r.raise_for_status()
        print(r.json())

    async with websockets.connect(WS) as ws:
        for seq in range(50):
            # 保持头部位姿示例；真实业务填完整 targets
            frame = {
                "cmd": "command",
                "seq": seq,
                "targets": {"astribot_head": [0.0, 0.0]},
                "check_step_delta": True,
                "return_state": False,
            }
            await ws.send(json.dumps(frame))
            ack = json.loads(await ws.recv())
            if not ack.get("accepted", True):
                print("rejected", ack)
                break
            await asyncio.sleep(0.02)

        await ws.send(json.dumps({"cmd": "close"}))
        print(await ws.recv())

asyncio.run(main())
```

帧字段：

| 字段 | 说明 |
|------|------|
| `targets` | `{ "astribot_torso": [...], ... }` 按 part 分组 |
| `q` + `layout` | 扁平向量 + part 顺序列表（默认轨迹 22 维 layout） |
| `names` + `poses` | `space=cartesian` 时使用 |
| `check_step_delta` | 默认 true；首帧对齐可先 `false` 或先 MoveTo |
| `return_state` | true 时 ack 带当前 state |

安全拒绝时 `accepted=false`，并带 `violations`（限位 / 单步过大等）。

### 5.3 音频流 ` /v1/ws/audio`

二进制帧布局（big-endian 头 + little-endian float32 PCM）：

```text
[u32 seq][u32 sample_rate][u16 channels][u16 format=0][float32 samples...]
```

```python
import asyncio
import json
import struct
import httpx
import websockets
import numpy as np

BASE = "http://192.168.0.10:8080"
WS = "ws://192.168.0.10:8080/v1/ws/audio"

def pack_frame(seq: int, samples: np.ndarray, sr=32000, ch=1) -> bytes:
    payload = np.asarray(samples, dtype=np.float32).tobytes()
    return struct.pack("!IIHH", seq, sr, ch, 0) + payload

async def main():
    with httpx.Client(base_url=BASE) as c:
        c.post("/v1/audio/stream/start", json={"force": True}).raise_for_status()

    # 1 秒 440Hz 正弦，按 100ms 切块
    sr, chunk_ms = 32000, 100
    n = sr  # 1s
    t = np.arange(n) / sr
    wave = 0.2 * np.sin(2 * np.pi * 440 * t)

    async with websockets.connect(WS) as ws:
        step = int(sr * chunk_ms / 1000)
        seq = 0
        for i in range(0, n, step):
            frame = pack_frame(seq, wave[i : i + step], sr=sr)
            await ws.send(frame)
            print(await ws.recv())
            seq += 1
            await asyncio.sleep(chunk_ms / 1000)

        await ws.send(json.dumps({"cmd": "stop"}))
        print(await ws.recv())

asyncio.run(main())
```

---

## 6. 关节 / Part 约定（客户端拼包必看）

Readable（查询默认，**25 DOF**）：

| part | DOF |
|------|-----|
| `astribot_chassis` | 3 |
| `astribot_torso` | 4 |
| `astribot_arm_left` | 7 |
| `astribot_gripper_left` | 1 |
| `astribot_arm_right` | 7 |
| `astribot_gripper_right` | 1 |
| `astribot_head` | 2 |

轨迹播放内部一般不含底盘（**22 DOF**）：去掉 `astribot_chassis`。

夹爪量纲：SDK 原始值约 `0–100`（不是弧度）。

`targets` 示例：

```json
{
  "astribot_torso": [0, 0, 0, 0],
  "astribot_arm_left": [0, 0, 0, 0, 0, 0, 0],
  "astribot_gripper_left": [50],
  "astribot_arm_right": [0, 0, 0, 0, 0, 0, 0],
  "astribot_gripper_right": [50],
  "astribot_head": [0.0, 0.0]
}
```

---

## 7. 典型业务场景脚本

### 7.1 「意图 tag → 动作」状态机

```text
默认 play(idle, loop)
收到 wave → play(wave) → 结束后 holding
收到 speak → play(speak) → 循环说话
收到 idle → play(idle)
异常 → estop（人工）
```

客户端做 tag 映射与去重；Orin 在 `force=false` 时也会忽略同动作重复 play。

### 7.2 「先对齐再实时」

```text
1. POST /v1/motion/move_to/joints  wait=true  （对齐首帧）
2. POST /v1/motion/realtime/session force=true
3. WS /v1/ws/realtime 按 50Hz 发 command
4. POST /v1/motion/realtime/close
```

若直接 realtime 且步长过大，会被 safety 打回 `exceeds_max_step_delta`。

### 7.3 「主控制页面接管 -> bridge 失权 -> 恢复」

```text
1. GET /v1/control-rights
2. 若 have_control_rights=false：
   - 只读接口仍可继续用于诊断
   - 下一次写控制请求会默认自动尝试恢复一次
3. 若自动恢复成功：继续执行原请求
4. 若命中 cooldown 或恢复失败：
   - 看 GET /v1/control-rights
   - 必要时显式 POST /v1/control-rights/reacquire
5. 再执行 play / move_to / realtime
```

也可以在写请求里显式覆盖服务端默认策略：

```json
{ "reacquire_if_needed": false }
```

例如：

```bash
curl -s -X POST "$BRIDGE/v1/actions/play" \
  -H 'Content-Type: application/json' \
  -d '{"action_id":"idle","force":true}' | python3 -m json.tool
```

### 7.4 「边播动作边看状态」

开两个终端：一端 `WS state`，一端 `play wave`，观察 `mode` 与 `pos_flat` 变化。

---

## 8. 故障排查

| 现象 | 排查 |
|------|------|
| `curl` 超时 | Orin 是否启动 Bridge；IP/防火墙；同网段 |
| `/ready` ready=false | SDK/机器人未起；看 `reader_ready` / `control_ready` / `audio_ready` 哪一项失败 |
| `/ready` 本机都超时 | 进程半死或 SDK/ROS 路径卡住；先看 `/health`、`ps`、`ss -lntp`，必要时 `./scripts/restart_bridge.sh` |
| `control_rights_lost` | 主控制页面或其它控制端接管；先 `GET /v1/control-rights`，再视情况 `POST /v1/control-rights/reacquire` |
| `control_rights_cooldown` | bridge 刚自动恢复过一次；等待几秒后再试，或确认主页面已停止干扰 |
| 409 `control_busy` | 查 `GET /v1/status`；加 `force:true` 或先 stop/close |
| play 404 | `GET /v1/actions/` 核对 id；manifest 软链 |
| realtime 全被拒 | 检查 violations；减小步长或先 move_to |
| WS 立刻断开 | 用 `ws://` 不是 `http://`；确认端口 8080 |
| 音频无声 | 先 `GET /v1/audio/status` 看 `received_frames`/`published_frames`；ROS `ros2 topic info /astribot_audio/speaker/stream` 订阅数是否为 0；见 [API_REFERENCE.md](API_REFERENCE.md) §5.3 |
| HTTP 401 | 配置了 `http.api_key`，请求加 `X-API-Key` |
| 想查某次失败的完整链路 | 先记下响应头 `X-Request-Id`，再 `grep`/搜索 `logs/bridge.log` |

Orin 日志一般既会出现在启动 Bridge 的终端，也会写入 `logs/bridge.log`；也可浏览器打开 `/docs` 用 Swagger 单步点测。

---

## 9. 与旧 UDP Bridge 对照（迁移）

| 旧 UDP | 新 HTTP/WS |
|--------|------------|
| AST1 `:9900` play/list/status | `/v1/actions/*` |
| AST2 `:9901` + push `:9902` | `WS /v1/ws/state` + `GET /v1/joints` |
| AST3 `:9903` reset/step | realtime session + command/WS |
| 开发机 `action/client.py` | `clients/python/bridge_client.py` |

旧服务与新 Bridge **不要同时**用 `high_control_rights` 抢同一台机器人。

---

## 10. 一键冒烟脚本（开发机）

保存为 `smoke_test.py` 后执行：`python3 smoke_test.py http://192.168.0.10:8080`

```python
#!/usr/bin/env python3
import sys
import httpx

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"

def ok(path, method="GET", **kwargs):
    r = httpx.request(method, base + path, timeout=30.0, **kwargs)
    print(method, path, r.status_code)
    r.raise_for_status()
    body = r.json()
    assert body.get("ok"), body
    return body["data"]

print("== health ==")
ok("/health")
ready = ok("/ready")
print("ready:", ready.get("ready"))
print("== robot ==")
print(ok("/v1/robot").get("readable_dof"))
print("== joints ==")
j = ok("/v1/joints?fields=pos")
print("parts:", j.get("parts"))
print("== actions ==")
actions = ok("/v1/actions/")
print("count:", len(actions) if isinstance(actions, list) else actions)
print("== status ==")
print(ok("/v1/status").get("control"))
print("SMOKE OK")
```

安全提示：冒烟脚本默认**不**自动 play / estop / home，避免误动真机；运动类请人工确认环境后再测。

---

## 11. 相关链接

- **API 接入参考：** [`docs/API_REFERENCE.md`](API_REFERENCE.md)
- 服务端 README：仓库根目录 [`README.md`](../README.md)
- 交互式 API：`http://<ORIN_IP>:8080/docs`
- OpenAPI JSON：`http://<ORIN_IP>:8080/openapi.json`
- 默认配置：[`config/default.yaml`](../config/default.yaml)
