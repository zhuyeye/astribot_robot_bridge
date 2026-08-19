# Decisions

## 2026-08-19 - 使用根目录 tracking

- Context: 仓库需要一个稳定位置记录工作进展，供下次会话和跨仓库扫描复用。
- Decision: 在仓库根目录使用 `tracking/`，不放进 `docs/`。
- Why: `docs/` 留给 API 与客户端协议；根目录 `tracking/` 更方便工具扫描，也和其他仓库布局一致。
- Follow-up: 深度技术说明继续写在 `docs/`；进度只在 `tracking/` 更新。

## 2026-08-19 - system 音频走 Yundea Pulse，不走板载 APE

- Context: SDK speaker topic 曾无订阅者；Pulse 默认 sink 会漂到 `platform-sound`，或 Yundea 音量为 0%，导致 `paplay`/`pacat` 成功但听不到。
- Decision: `system` backend 按 sink 名匹配 `Yundea`，`paplay`/`pacat` 带 `--device`；启动时设默认 sink 并把音量设为 75%。音量仍用 `GET/POST /v1/audio/system-volume`。
- Why: 真机能听到的是 USB Yundea 8MICA，不是 Jetson 板载 APE。
- Follow-up: 改配置后需重启 Bridge。不要把 APE mixer 当成 system 出声路径。

## 2026-08-12 - 用 session_id 做运动 fencing

- Context: speak 连续打断时，迟到的 `play(idle)` / `realtime/close` / `move_to` 会污染新会话并导致跳变或挂掉。
- Decision: Bridge 维护 `ControlContext`；操作已有会话必须带 `session_id`，启动新动作可用 `expected_current_session_id`；stale 请求 no-op 并返回 `stale_session`。
- Why: 真机侧同一时刻只能一种控制模式，但 HTTP 到达顺序无法保证；必须按会话代数丢弃过期请求。
- Follow-up: 客户端按 [docs/CLIENT_SESSION_PROTOCOL.md](../docs/CLIENT_SESSION_PROTOCOL.md) 改调用。
