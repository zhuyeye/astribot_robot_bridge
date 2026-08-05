# Astribot Robot Bridge — API 接入参考

面向**第三方客户端 / 上位机**的 HTTP + WebSocket 接口说明。客户端无需安装 Astribot SDK，只要能访问 Orin 上的 Bridge 服务即可。

| 项 | 说明 |
|----|------|
| Base URL | `http://<ORIN_IP>:8080` |
| OpenAPI（可交互） | `http://<ORIN_IP>:8080/docs` |
| OpenAPI JSON | `http://<ORIN_IP>:8080/openapi.json` |
| 联调与 curl 示例 | [CLIENT_TEST_GUIDE.md](CLIENT_TEST_GUIDE.md) |
| Python 薄客户端 | `clients/python/bridge_client.py` |

---

## 1. 接入前须知

### 1.1 能力边界

| 能力 | 协议 | 说明 |
|------|------|------|
| 健康 / 就绪 | HTTP GET | 不依赖高控制权 SDK 的深度调用（`/health` 最轻） |
| 本体信息查询 | HTTP GET/POST | 关节、笛卡尔、FK/IK、相机元数据等 |
| 状态订阅 | WebSocket | 周期性推送关节等字段 |
| 预存动作 | HTTP POST | HDF5 轨迹播放（`action_id` 或 tag） |
| MoveTo | HTTP POST | 平滑到达关节/笛卡尔/home/路点 |
| 实时控制 | HTTP + WebSocket | 高频 `set_*`（需先开会话） |
| 音频 clip | HTTP POST | 本地 wav |
| 音频流 | HTTP + WebSocket | PCM 二进制帧 |
| 系统音量 | HTTP | `pactl` / `amixer`（与流式播放不同层） |

**重要：** `POST /v1/actions/play`（例如 `speak`）只负责**肢体轨迹**，不会自动播放 TTS/PCM。说话需单独调用 `/v1/audio/*` 或 `WS /v1/ws/audio`。

运动与音频在 Bridge 内使用不同 SDK 实例，可并行；但音频会话自身互斥（同时只能一路 clip 或一路 stream，除非 `force`）。

### 1.2 运动模式互斥

任意时刻 Bridge 内部只有一种运动模式持有控制面：

`idle` | `trajectory` | `move_to` | `realtime`

冲突时返回 HTTP **409** `control_busy`，可在请求体中加 `"force": true` 抢占（`estop` 可打断任意模式）。

### 1.3 机器人控制权（与主控页）

Bridge 默认托管控制权。写控制接口前会检查是否仍拥有机器人控制权；失权时可能自动尝试重抢（受配置冷却与次数限制）。查询与重抢：

- `GET /v1/control-rights`
- `POST /v1/control-rights/reacquire`

信息查询走只读 SDK 实例，失权时仍尽量可用。

---

## 2. 协议约定

### 2.1 统一 JSON 响应

成功：

```json
{ "ok": true, "data": { }, "error": null }
```

失败（HTTP 4xx/5xx，body 仍为 JSON）：

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

### 2.2 认证（可选）

若 `config/default.yaml` 中设置了 `http.api_key`，所有 HTTP 请求需带：

```http
X-API-Key: <your-key>
```

WebSocket 当前不校验 API Key（按部署环境为准）。

### 2.3 请求追踪

每个 HTTP 响应头包含 **`X-Request-Id`**。异常排查时在 Orin 上检索 `logs/bridge.log` 中同一 id。

### 2.4 控制类语义字段

许多控制接口在 `data` 中额外返回：

| 字段 | 含义 |
|------|------|
| `resource` | 如 `motion.move_to.joints`、`actions.playback`、`audio.stream` |
| `execution` | `sync`：本请求内完成或仅切换状态；`async`：后台执行 |
| `operation_status` | 如 `accepted`、`opened`、`applied`、`stopped`、`succeeded` |

HTTP **202**：已接收并开始后台任务，**不表示动作已结束**。MoveTo 异步模式会返回 `task_id`，可 `GET /v1/motion/tasks/{task_id}` 轮询。

### 2.5 写控制公共字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `force` | bool | 抢占当前运动模式 |
| `reacquire_if_needed` | bool \| null | 失权时是否尝试重抢控制权；`null` 用服务端默认 |
| `wait` | bool | MoveTo：true 同步等待完成；false 返回 202 + task |

---

## 3. 接口索引

### 3.1 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 进程存活，不查 SDK |
| GET | `/ready` | reader/control/audio 就绪 + 控制权 + 音频 worker 摘要 |
| GET | `/v1/status` | 控制面、轨迹、音频、控制权快照 |

### 3.2 控制权

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/control-rights` | 当前控制权状态 |
| POST | `/v1/control-rights/reacquire` | 显式重抢 |

### 3.3 信息（只读）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/robot` | 本体元信息、DOF、parts |
| GET | `/v1/joints` | Query: `names`, `which=current\|desired`, `fields=pos,vel,...` |
| GET | `/v1/joints/limits` | Query: `names`（逗号分隔） |
| GET | `/v1/cartesian` | Query: `names`, `frame`, `which=current\|desired\|wbc` |
| POST | `/v1/kinematics/fk` | Body: `names`, `joints` |
| POST | `/v1/kinematics/ik` | Body: `names`, `poses` |
| POST | `/v1/safety/closest-point` | Body: `torso`, `arm_left`, `arm_right` 关节（可选） |
| GET | `/v1/cameras` | 相机元数据（不出图） |

### 3.4 预存动作

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/actions/` | 动作列表 |
| GET | `/v1/actions/status` | 播放状态 |
| GET | `/v1/actions/{action_id}` | 单个动作元数据 |
| POST | `/v1/actions/play` | 播放；tag 会随机解析到具体动作 |
| POST | `/v1/actions/stop` | 停止 |

### 3.5 运动

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/motion/tasks/{task_id}` | 异步 MoveTo 任务状态 |
| POST | `/v1/motion/move_to/joints` | 关节 MoveTo |
| POST | `/v1/motion/move_to/cartesian` | 笛卡尔 MoveTo |
| POST | `/v1/motion/move_to/home` | 回 home |
| POST | `/v1/motion/move_to/waypoints` | 路点 |
| POST | `/v1/motion/realtime/session` | 开启实时会话 |
| POST | `/v1/motion/realtime/command` | 实时指令（低频调试用） |
| POST | `/v1/motion/realtime/close` | 关闭实时会话 |
| POST | `/v1/motion/gripper/open` | 夹爪 |
| POST | `/v1/motion/gripper/close` | 夹爪 |
| POST | `/v1/motion/settings` | 滤波、头跟随、**collision_avoidance** 等 |
| POST | `/v1/motion/estop` | 急停 |
| POST | `/v1/motion/restart` | 重启控制相关状态（按 SDK） |

### 3.6 音频

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/audio/clips` | 数据集 wav 列表 |
| GET | `/v1/audio/status` | 播放/流状态、队列、worker |
| GET | `/v1/audio/system-volume` | 系统音量 |
| POST | `/v1/audio/play` | 播放 clip |
| POST | `/v1/audio/stop` | 停止并 deactivate |
| POST | `/v1/audio/stream/start` | 准备接收 WS PCM |
| POST | `/v1/audio/stream/stop` | 同 stop |
| POST | `/v1/audio/system-volume` | 设置系统音量 |

### 3.7 WebSocket

| 路径 | 说明 |
|------|------|
| `WS /v1/ws/state` | 状态流；Query: `hz`, `fields` |
| `WS /v1/ws/realtime` | 实时控制 JSON 命令 |
| `WS /v1/ws/audio` | PCM 二进制帧 + 文本 start/stop |

---

## 4. 常用请求体示例

### 4.1 播放动作

```http
POST /v1/actions/play
Content-Type: application/json

{
  "action_id": "speak",
  "request_id": "client-session-001",
  "force": true,
  "reacquire_if_needed": true
}
```

响应常为 **202**，`data` 含 `action_id`（解析后）、`generation` 等。

### 4.2 MoveTo 关节（异步）

```json
{
  "targets": {
    "astribot_head": [0.0, 0.0]
  },
  "duration": 2.0,
  "use_wbc": false,
  "wait": false,
  "force": false
}
```

### 4.3 实时会话 + 关节指令

```json
POST /v1/motion/realtime/session
{ "rate_hz": 50, "space": "joints", "control_way": "filter", "force": true }

POST /v1/motion/realtime/command
{ "targets": { "astribot_head": [0.1, 0.0] }, "check_step_delta": true }
```

高频闭环请用 **`WS /v1/ws/realtime`**，不要用 REST 逐帧。

实时关节下发默认 `use_wbc=false` 的直接关节跟踪；`collision_avoidance` 主要作用于 WBC 模式，对纯关节 `set` 帮助有限。

### 4.4 运动设置（自碰撞规避）

```json
POST /v1/motion/settings
{
  "collision_avoidance": true,
  "head_follow": false,
  "filter_scale": null
}
```

对应 SDK `set_wbc_collision_avoidance`。与下发的 `qpos` 无协议绑定；仅在 WBC 执行路径上影响实际运动。

### 4.5 音频 clip

```json
POST /v1/audio/play
{ "clip_id": "hello", "mode": "service", "force": true }
```

`mode`: `service`（wav 服务）| `stream`（topic 分块）。

### 4.6 系统音量

```json
POST /v1/audio/system-volume
{ "volume_percent": 60, "unmute": true }
```

建议在**开始流式播放之前**设好音量；播放过程中改音量可能影响底层 sink，必要时 `stop` 后重新 `stream/start`。

---

## 5. WebSocket 协议

### 5.1 状态流 `GET` → `WS /v1/ws/state`

连接示例：

```text
ws://<ORIN>:8080/v1/ws/state?hz=50&fields=joints.pos,joints.vel
```

服务端周期性推送 JSON，含 `seq` 及所请求字段。`hz` 受配置上限约束。

### 5.2 实时控制 `WS /v1/ws/realtime`

文本 JSON，常用字段：

| `cmd` / 行为 | 说明 |
|--------------|------|
| `{"cmd":"open", "rate_hz":50, "space":"joints", "force":true}` | 开会话 |
| `{"cmd":"command", "targets":{...}, "seq":1}` | 下发（与 REST command 类似） |
| `{"cmd":"close"}` | 关闭 |
| `{"cmd":"ping"}` | 心跳 |

断开连接后 Bridge 会尝试关闭 realtime 会话。

### 5.3 音频流 `WS /v1/ws/audio`

**推荐顺序：**

1. `POST /v1/audio/stream/start`（或 WS 发 `{"cmd":"start","force":true}`）
2. 连接 `WS /v1/ws/audio`
3. 发送二进制帧
4. `POST /v1/audio/stop` 或 WS `{"cmd":"stop"}`

**二进制帧格式（小端 payload，大端头）：**

```text
[u32 seq BE][u32 sample_rate BE][u16 channels BE][u16 format=0 BE][float32 PCM...]
```

`format=0` 表示 float32 PCM。Bridge 入队后由 worker 发布到 `/astribot_audio/speaker/stream`。

**排查无声：** 在 Orin 上确认 ROS topic 有订阅者：

```bash
source <SDK>/env.sh
ros2 topic info /astribot_audio/speaker/stream -v
```

若 `Subscription count: 0`，说明机器人扬声器播放节点未起来，需先 `activate_audio` / 系统 audio 服务，而非 Bridge 未收到数据。`GET /v1/audio/status` 可看 `received_frames` / `published_frames`。

---

## 6. 推荐接入流程

### 6.1 最小连通

1. `GET /health`
2. `GET /ready` → `data.ready == true`
3. `GET /v1/robot` 或 `GET /v1/joints`

### 6.2 「说话」业务（动作 + 音频）

1. （可选）`POST /v1/audio/system-volume`
2. `POST /v1/audio/stream/start`
3. 连接 `WS /v1/ws/audio` 并推送 PCM
4. 并行 `POST /v1/actions/play` `{"action_id":"speak",...}`
5. 结束：`POST /v1/audio/stop`，`POST /v1/actions/stop` 或 `idle`

不要只发 `speak` 动作而期望自动出声。

### 6.3 Teleop / 策略高频控制

1. `POST /v1/motion/realtime/session`
2. `WS /v1/ws/realtime` 或高频 command（WS 优先）
3. `POST /v1/motion/realtime/close`

### 6.4 状态机 + 意图 tag

1. `WS /v1/ws/state` 持续订阅
2. 意图 → `POST /v1/actions/play`（tag 如 `wave`、`speak`）
3. 冲突时查 `GET /v1/status` 的 `control.holder`

---

## 7. 常见错误码

| code | HTTP | 处理建议 |
|------|------|----------|
| `control_busy` | 409 | `force` 或先 stop/close 当前模式 |
| `control_rights_lost` | 409 | `GET /v1/control-rights`，必要时 `reacquire` |
| `control_rights_cooldown` | 409 | 等待冷却后重试 |
| `unknown_action` | 404 | `GET /v1/actions/` |
| `realtime_inactive` | 409 | 先 `realtime/session` |
| `audio_busy` | 409 | `force` 或 `audio/stop` |
| `stream_inactive` | 409 | 先 `audio/stream/start` |
| `audio_backpressure` | 429 | 降低发送帧率或增大间隔 |
| `not_ready` | 200 body `ok:false` | 查 `reader_ready` / `control_ready` / `audio_ready` |
| `unauthorized` | 401 | 检查 `X-API-Key` |
| `system_volume_unavailable` | 503 | 无 pactl/amixer 会话 |

---

## 8. 版本与变更

- 服务版本见 OpenAPI `info.version`（当前应用约 `0.1.0`）。
- 行为与默认值以仓库内 `config/default.yaml` 及 `bridge/schemas/requests.py` 为准。
- 机器可读契约：启动服务后访问 `/openapi.json`。

---

## 9. 相关文档

- [CLIENT_TEST_GUIDE.md](CLIENT_TEST_GUIDE.md) — curl / Python / 冒烟脚本、故障排查
- [../README.md](../README.md) — 部署与目录结构
