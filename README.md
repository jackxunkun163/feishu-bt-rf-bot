# MTK 蓝牙射频参数编译机器人（飞书）

有人在飞书里给机器人发送 BT RF 参数后，机器人会自动完成以下步骤：

1. 校验参数（参数名、范围、个数）→ 2. 修改源码 → 3. 编译 → 4. 回复结果（diff、耗时、产物或错误摘要，失败时附上完整日志）→ 5. 还原源码

```
项目=k6789
Radio[0]=0x07
TxPWOffset=0x80,0x82,0x80
```

## 部署（在编译服务器上运行）

### 1. 创建飞书应用
1. 打开 [飞书开放平台](https://open.feishu.cn/app)，创建「企业自建应用」，在「添加应用能力」中添加**机器人**。
2. 在「权限管理」中开通以下权限：
   - `im:message`（获取与发送单聊、群组消息）
   - `im:message.p2p_msg:readonly`（读取用户发给机器人的单聊消息）
   - `im:message.group_at_msg:readonly`（接收群聊中 @机器人 的消息）
   - `im:resource`（上传文件，用于发送编译日志）
3. 在「事件与回调」→「事件配置」中，订阅方式选择**使用长连接接收事件**，添加事件 `im.message.receive_v1`。
   注意：需要先把机器人启动起来，长连接方式才能保存成功。
4. 发布应用版本，然后把机器人拉进群，或者直接单聊。

### 2. 安装与配置
```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 填写 app_id/app_secret、源码路径、编译命令、参数规则
```

### 3. 本地验证（不连飞书）
```bash
python -m btbot.cli -c config.yaml "/params"
python -m btbot.cli -c config.yaml "/check Radio[0]=0x07"
```

### 4. 启动
```bash
python -m btbot.main -c config.yaml --check   # 自检：源码目录、参数文件、定位规则
python -m btbot.main -c config.yaml
```
长期运行建议使用 systemd，参考 `btbot.service`（崩溃自动重启，停止时预留足够时间还原源码）。

## 参数规则（config.yaml → projects.<项目>.params）

| kind | 适用场景 | 关键字段 |
|---|---|---|
| `c_array` | `CFG_BT_Default.h` 里 `/* Radio */ {0x06, 0x80, ...}` 这类数组 | `anchor`：定位数组的正则；`index`：可选，固定修改某个元素 |
| `kv` | `bt.cfg` / `WMT_SOC.cfg` 这类 `key=value` 文件 | `key` |
| `regex` | 其它任意文本 | `pattern`：必须恰好一个捕获组 |

通用字段：
- `type`：`byte`（默认，0~255，写成 `0xNN`）/ `int` / `float` / `string`
- `min` / `max`：取值范围
- `aliases`：中文别名
- `desc`：说明

只有明确配置过的参数才能修改。只要有一项校验失败，所有文件都不会改动。

## 命令
`/help`、`/params [项目]`、`/check <参数>`（只预览不编译）、`/status`（查看队列）、`/cancel`（取消自己的任务）、`/unblock`（管理员在人工处理后恢复接单）

## 健壮性设计
| 场景 | 处理方式 |
|---|---|
| 参数写错、越界、`nan` 等非法值，或同一参数给了两个不同的值 | 拒绝并说明原因，**一个文件都不改** |
| 规则定位不唯一（anchor/key 匹配到多处），或 anchor 后面不是数组 | 拒绝修改，提示把规则写精确，避免改错位置 |
| 写文件中途断电 | 原子写（临时文件 + rename），不会出现写了一半的文件 |
| 编译中机器人崩溃 / 被 kill / 断电 | 改源码前先备份并写 `journal.json`；重启后先清理残留编译进程（核对进程启动时间，防止 PID 复用后误杀），再还原源码，被中断的任务自动重新排队（重试次数可配置） |
| 正常停止（SIGTERM / Ctrl+C） | 终止编译、还原源码，当前任务保留在队列中，重启后继续 |
| 编译期间有人手动改了同一个文件 | 不覆盖对方的修改，只告警；机器人暂停接单，人工确认后发送 `/unblock` 恢复 |
| 源码还原失败 | 暂停接单并告警，绝不在脏源码上继续编译 |
| 排队任务 | 持久化到 `queue.json`，重启不丢 |
| 飞书重复推送、重启后补推旧消息 | message_id 去重记录持久化，并忽略超过 N 分钟的旧消息 |
| 飞书 API 网络错误或限流 | 指数退避重试；卡片发送失败时降级为纯文本；日志上传失败时回复服务器上的路径 |
| 长连接断开 | SDK 自动重连，外层循环兜底重连 |
| 编译超时、取消 | 杀掉整个进程组，源码自动还原 |
| 编译日志过大 | 先 gzip 压缩，压缩后仍超过 30MB 则只上传日志末尾 |
| 磁盘空间不足 | 编译前检查 `min_free_gb`，不足时直接拒绝并告警 |
| 有人滥用、刷请求 | 白名单、每人任务数上限、队列总数上限、消息长度上限 |
| 同时启动了两个机器人进程 | 文件锁保证单实例 |
| 消息处理代码出现 bug | 每条消息独立捕获异常，回复出错信息并告警，不影响服务 |

## 注意
- 同一时间只编译一个任务，其余排队。执行前会基于当时的源码重新校验一次。
- `restore_after_build: true`（默认）：每次编译后都会还原源码，各请求互不影响。
- NVRAM 默认值（`CFG_BT_Default.h`）只在 NVRAM 为空时生效，刷机时需要清除 NVRAM 或 Format All。
- 生产环境建议配置 `allowed_users` 白名单。
- `data/` 目录内容：`bot.log`（滚动保存）、`jobs/<任务ID>/`（diff、编译日志、原文件备份）、`history.jsonl`、`queue.json`、`journal.json`。
- 配置 `alert_chat_id` 后，崩溃恢复、还原失败、磁盘不足等情况会推送到管理员群。

## 测试与 CI
```bash
pip install pytest
python -m pytest tests
```
GitHub Actions（`.github/workflows/ci.yml`）在 push 到 main 和提交 PR 时自动运行全部测试，覆盖 Ubuntu（Python 3.9、3.12）和 Windows。测试内容包括参数解析与校验、文件修改、编译成功/失败、取消、优雅退出、崩溃恢复、单实例锁、进程组清理等。
实际修改源码和编译在你们自己的编译服务器上进行，CI 用模拟的源码目录和编译脚本，不需要 Android 源码。
