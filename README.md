# Astribot Robot Bridge

Orin 上运行的统一 HTTP / WebSocket 机器人桥接服务。开发机无需安装 Astribot SDK，通过 REST 查询本体信息、控制运动，并通过 WebSocket 订阅状态或下发实时动作。

旧版 UDP bridge（`remote_control_bridge_from_202`）保持不变；本仓库是并列的新实现。

## 能力概览

| 域 | 接口 | 说明 |
|----|------|------|
| 系统 | `GET /health` `GET /ready` `GET /v1/status` | 健康与控制权状态 |
| 控制权 | `GET /v1/control-rights` `POST /v1/control-rights/reacquire` | 查询/重抢机器人控制权 |
| 信息 | `GET /v1/robot` `/joints` `/cartesian` `/cameras` … | SDK 可读信息封装 |
| 状态流 | `WS /v1/ws/state` | 关节等状态推送 |
| 预存动作 | `/v1/actions/*` | HDF5 轨迹播放（替代 AST1） |
| MoveTo | `/v1/motion/move_to/*` | 丝滑到达 joints/cartesian/home/waypoints |
| 实时控制 | `/v1/motion/realtime/*` + `WS /v1/ws/realtime` | 高频 `set_*`（替代 AST3） |
| 急停/夹爪 | `/v1/motion/estop` `/gripper/*` | 安全与夹爪 |
| 音频 | `/v1/audio/*` + `WS /v1/ws/audio` | clip 播放 + PCM 流 |

统一响应：

```json
{ "ok": true, "data": { }, "error": null }
```

控制类接口会额外返回：

- `resource`: 操作目标，例如 `motion.move_to.joints`
- `execution`: `sync` 或 `async`
- `operation_status`: 如 `accepted` / `opened` / `applied` / `stopped` / `succeeded`

异步接口会返回 HTTP `202`，表示服务端已接收并开始后台执行，不代表动作已经完成。

任意时刻只有一种运动模式持有控制权：`idle` / `trajectory` / `move_to` / `realtime`。冲突返回 HTTP 409（可用 `force=true` 抢占）。`estop` 可打断任意模式。

机器人主控制页面如果接管控制权，bridge 会进入失权状态。此时信息读取仍尽量通过只读实例继续工作；写控制接口会默认由服务端自动尝试恢复一次控制权，并受冷却时间与单次失权最大重抢次数限制，避免频繁切换。

## 快速开始（Orin）

```bash
cd /home/astribot/workspace/astribot_robot_bridge
python3 -m pip install -r requirements.txt

# 需能访问 SDK；启动脚本会 source env.sh
./scripts/start_bridge.sh
# 默认监听 0.0.0.0:8080
```

`start_bridge.sh` 现在会先检查监听端口是否已经被旧进程占用，避免重复启动后表面成功、实际请求打到旧进程。

标准化运维脚本：

```bash
./scripts/stop_bridge.sh
./scripts/restart_bridge.sh
# 如旧进程不响应，可用：
./scripts/restart_bridge.sh --force-stop
```

OpenAPI 文档：`http://<orin-ip>:8080/docs`

动作库默认软链到旧仓库：

```
assets/actions_link -> ../remote_control_bridge_from_202/actions
```

可在 `config/default.yaml` 修改 `actions.manifest`。

## API 文档（接入）

第三方接入请以 API 参考为准（端点、请求体、WebSocket 帧格式、动作与音频分离说明）：

**[docs/API_REFERENCE.md](docs/API_REFERENCE.md)**

联调、curl 示例与故障排查：

**[docs/CLIENT_TEST_GUIDE.md](docs/CLIENT_TEST_GUIDE.md)**

## 开发机示例

```bash
# 复制 clients/python/bridge_client.py 即可
python3 clients/python/bridge_client.py --base-url http://192.168.0.10:8080 health
python3 clients/python/bridge_client.py --base-url http://192.168.0.10:8080 list-actions
python3 clients/python/bridge_client.py --base-url http://192.168.0.10:8080 play wave
```

```python
from clients.python.bridge_client import BridgeClient  # or copy module

with BridgeClient("http://192.168.0.10:8080") as c:
    print(c.robot())
    print(c.joints())
    c.play("idle")
```

### WebSocket 状态流

```python
import asyncio, websockets, json

async def main():
    uri = "ws://192.168.0.10:8080/v1/ws/state?hz=50&fields=joints.pos,joints.vel"
    async with websockets.connect(uri) as ws:
        for _ in range(10):
            print(json.loads(await ws.recv())["seq"])

asyncio.run(main())
```

### 实时控制

```bash
# 1) 开会话
curl -X POST http://ORIN:8080/v1/motion/realtime/session \
  -H 'Content-Type: application/json' \
  -d '{"rate_hz":50,"space":"joints"}'

# 2) 下发（或走 WS /v1/ws/realtime）
curl -X POST http://ORIN:8080/v1/motion/realtime/command \
  -H 'Content-Type: application/json' \
  -d '{"targets":{"astribot_head":[0.0,0.0]}}'

# 3) 关闭
curl -X POST http://ORIN:8080/v1/motion/realtime/close
```

### 音频

- **Clip**：`POST /v1/audio/play` `{"clip_id":"hello"}`（读取 `audio.dataset_dir`）
- **系统音量**：`GET /v1/audio/system-volume`，`POST /v1/audio/system-volume` `{"volume_percent":60}`
- **流式**：`POST /v1/audio/stream/start`，再连 `WS /v1/ws/audio` 发二进制帧：

```
[u32 seq][u32 sample_rate][u16 channels][u16 format=0][float32 PCM...]
```

## 配置

见 [`config/default.yaml`](config/default.yaml)。环境变量：

| 变量 | 说明 |
|------|------|
| `ASTRIBOT_SDK_ROOT` | SDK 路径 |
| `BRIDGE_CONFIG` | yaml 配置路径 |
| `BRIDGE_HOST` / `BRIDGE_PORT` | 覆盖监听地址 |

可选 `http.api_key`：启用后请求需带 `X-API-Key`。

日志默认同时输出到终端和 `logs/bridge.log`，可在 `logging.file`、`logging.rotate_mb`、`logging.backup_count` 中调整。每个 HTTP 请求都会带 `X-Request-Id` 响应头，便于按请求串联日志。

## 目录结构

```
astribot_robot_bridge/
├── bridge/           # FastAPI 服务
├── config/           # 默认配置
├── scripts/          # start_bridge.sh
├── assets/           # 动作软链
└── clients/python/   # 薄客户端
```

## Git 版本管理

仓库根目录为 `astribot_robot_bridge`（与 `remote_control_bridge_from_202` 并列，各自独立 git）。

```bash
cd /home/astribot/workspace/astribot_robot_bridge

# 首次（若尚未 init）
git init
git add .
git status   # 确认未纳入 logs/、__pycache__ 等

git commit -m "Initial astribot robot bridge."

# 关联远程（按你们 Git 服务器地址修改）
git remote add origin <YOUR_GIT_URL>
git branch -M main
git push -u origin main
```

说明：

- `assets/actions_link` 是指向旧 repo 动作库的**符号链接**，git 会记录链接本身，不会复制 HDF5 大文件。
- `logs/`、`run/` 已在 `.gitignore` 中忽略。
- 日常：`git pull` → 改代码 → `git commit` → `git push`；Orin 上部署可 `git pull` 后 `./scripts/restart_bridge.sh`。

## 与旧 UDP Bridge 对照

| 旧 | 新 |
|----|----|
| AST1 `:9900` play | `POST /v1/actions/play` |
| AST2 state push | `WS /v1/ws/state` |
| AST3 step | realtime session + command/WS |
| AST4 audio（设计） | `/v1/audio` + `WS /v1/ws/audio` |
