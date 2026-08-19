---
id: astribot-robot-bridge
status: active
updated: 2026-08-19
phase: session-and-system-audio
related:
  - remote_control_bridge_from_202
---

# Overview

## 项目用途

Orin 上的 HTTP / WebSocket 机器人桥接服务。开发机不装 Astribot SDK，通过 REST 查本体、控运动，通过 WebSocket 订状态或下发实时动作；音频可走 SDK speaker 或本机 Pulse。

旧版 UDP bridge（`remote_control_bridge_from_202`）保持不变；本仓库是并列的新实现。

## 当前阶段

当前阶段是 **session-and-system-audio**：运动会话 fencing 和 Yundea 系统播音已经合进代码。做到「真机重启后 system 音频稳定出声，且客户端按 session 协议编排 speak/idle」就算这一阶段完成。

## 目标

- [x] 规范化 REST + WebSocket Bridge（查询 / 轨迹 / MoveTo / realtime / 音频）
- [x] 运动会话 fencing，避免迟到 idle/stop/close 污染新会话
- [x] realtime qpos 插值到 control_hz，并跟最新目标
- [x] system 音频钉到 Yundea USB，启动音量 75%，仍可用 API 调音量
- [ ] 重启 Bridge 后在真机验证 Yundea 出声
- [ ] 客户端按 session 协议改完并回归 speak 打断场景

## 工作主线

- 实现 / 开发: FastAPI `bridge/`，运动仲裁与 `ControlContext`，Pulse `system` 音频
- 数据 / 输入: `assets/actions_link` 轨迹库、`audio_dataset` wav、客户端 PCM 流
- 验证 / 运维: `scripts/start_bridge.sh` 等，Orin 真机联调，`logs/bridge.log`
- 文档 / 笔记: `docs/API_REFERENCE.md`、`docs/CLIENT_SESSION_PROTOCOL.md`、本目录

## 里程碑

- 已完成: 基础 Bridge、控制权恢复、session fencing、realtime 插值、Yundea system 音频路径
- 进行中: 真机确认 system 播放；tracking 初始化
- 下一步: 重启服务验证喇叭；推动客户端接入 session 协议

## 入口与参考

- 对外说明: [README.md](../README.md)
- API 接入: [docs/API_REFERENCE.md](../docs/API_REFERENCE.md)
- 客户端会话协议: [docs/CLIENT_SESSION_PROTOCOL.md](../docs/CLIENT_SESSION_PROTOCOL.md)
- 联调与排障: [docs/CLIENT_TEST_GUIDE.md](../docs/CLIENT_TEST_GUIDE.md)
- 日志约定: [docs/LOGGING_GUIDE.md](../docs/LOGGING_GUIDE.md)
- 配置: [config/default.yaml](../config/default.yaml)
