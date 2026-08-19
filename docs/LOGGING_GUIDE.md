# Astribot Robot Bridge — 日志设计说明

本文说明 Bridge 当前建议保留哪些日志，以及本仓库已经落到代码里的关键日志点，方便后续分析：

- 强制停止
- stale request 污染
- `idle` / `close` / `move_to` 竞态
- realtime 首帧跳变
- WS 旧连接误关新会话

---

## 1. 日志目标

日志需要能回答 4 个问题：

1. 客户端到底发了什么请求
2. Bridge 当时认为“当前控制上下文”是谁
3. 真机端最终执行了什么控制动作
4. 底层 SDK / ROS 为什么报错或退出

---

## 2. 必须保留的日志层

## 2.1 HTTP / WS 请求日志

已有：

- `bridge/app.py`
- `bridge/api/ws_state.py`

建议字段：

- `request_id`
- `session_id`
- `expected_current_session_id`
- `supersedes_session_id`
- path / cmd
- client ip
- 结果摘要

## 2.2 控制上下文日志

新增于：

- `bridge/domain/control_context.py`

覆盖事件：

- `session_issued`
- `transient_started`
- `transient_finished`
- `session_terminated`
- `terminate_skipped`
- `cleared_for_estop`

关键字段：

- `active_session_id`
- `active_mode`
- `active_epoch`
- `last_terminal_session_id`

## 2.3 playback / move_to / realtime 生命周期日志

新增于：

- `bridge/domain/trajectory_service.py`
- `bridge/domain/motion_service.py`

覆盖事件：

- play 请求受理 / stale / preempt / 停止 / 自然完成
- `move_to` 请求 / 执行 / 完成
- realtime open / close / stale / command reject / command accepted
- realtime worker 启停 / set_joints 异常

## 2.4 SDK / ROS / 驱动日志

建议同时保存：

- `logs/bridge.log`
- ROS node 日志
- SDK 错误码 / EtherCAT / heartbeat / sudden change

Bridge 侧日志负责还原“控制面发生了什么”，驱动侧日志负责解释“机器人为什么停了”。

---

## 3. 现在已经新增的关键日志点

## 3.1 control context

文件：`bridge/domain/control_context.py`

- 每次新 `playback` / `realtime` 会话发放时记 `session_issued`
- 每次 `move_to` 临时上下文进入 / 退出时记 `transient_started` / `transient_finished`
- 每次 session 终止时记 `session_terminated`
- stale 终止尝试记 `terminate_skipped`

## 3.2 trajectory

文件：`bridge/domain/trajectory_service.py`

- `trajectory play requested`
- `trajectory play accepted`
- `trajectory stale_play`
- `trajectory stale_stop`
- `trajectory first_frame_transition`
- `trajectory frame_loop_started`
- `trajectory play preempted`
- `trajectory stopped`
- `trajectory playback finished`
- `trajectory playback error`

## 3.3 motion / realtime

文件：`bridge/domain/motion_service.py`

- `motion move_to_* requested`
- `motion move_to_* executing/completed`
- `motion realtime_open requested/opened`
- `motion realtime_close requested/closed`
- `motion realtime_command_rejected`
- `motion realtime_command_accepted`
- `motion stale_request`
- `motion control_rights_lost`
- realtime worker started/stopped

## 3.4 websocket

文件：`bridge/api/ws_state.py`

- `realtime ws connected`
- `realtime ws open`
- `realtime ws close`
- `realtime ws command_rejected`
- `realtime ws disconnected`
- `realtime ws cleanup_close`
- `realtime ws cleanup_skip`

---

## 4. 推荐分析顺序

出问题后建议按这个顺序查：

1. 先看 `logs/bridge.log` 的 `request_id`
2. 找对应 `session_id`
3. 看 `control_context` 何时切到新 session
4. 看旧请求是否被 `stale_session` 丢掉
5. 看 `move_to` / `realtime` 的目标摘要和首帧 delta
6. 最后对照 ROS / SDK 错误码

---

## 5. 当前设计的取舍

为了日志可读，不会把 realtime 每一帧完整关节值全量落盘。

当前策略是：

- 每次 open / close 必打
- 第 1 帧和周期性帧打摘要
- safety reject 必打
- stale 必打
- worker 异常必打

这样日志量不会爆炸，但足够还原大部分故障现场。

---

## 6. 后续可选增强

如果后面还想继续加强日志，可以再加：

1. 异常窗口高频 dump
2. `session_id -> request_id` 专门索引
3. 事故前后状态快照环形缓冲
4. 音频 session 与 motion session 的联合时序日志
