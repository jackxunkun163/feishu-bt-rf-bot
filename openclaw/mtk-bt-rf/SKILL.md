---
name: mtk-bt-rf
description: 修改 Android MTK 平台蓝牙射频（BT RF）参数并通过 Jenkins 编译，把结果告诉提交人。当用户提供蓝牙射频参数（如 Radio、TxPWOffset、BT TX 功率）要求修改/编译，或查询可修改的参数、编译进度、取消编译时使用。
metadata: {"openclaw":{"requires":{"bins":["python3"],"env":["JENKINS_URL","JENKINS_USER","JENKINS_TOKEN","JENKINS_JOB"]},"primaryEnv":"JENKINS_TOKEN"}}
---

# MTK 蓝牙射频参数编译

修改源码和编译都在 Jenkins 的编译机上完成；你负责把用户的需求整理成标准参数、调用脚本、把结果反馈给用户。
脚本：`python3 {baseDir}/scripts/jenkins_rf.py`（只依赖 Python 标准库）。

## 参数格式

每行一个，名称必须是 Jenkins 规则文件里定义的参数名或别名（不区分大小写）：

```
Radio[0]=0x07                  # 数组的某个元素
TxPWOffset=0x80,0x82,0x80      # 一次给出数组全部元素
BtTxPower=9                    # 单个值
```

- 数值保持用户给的写法（十六进制 `0x07` 或十进制 `7` 都可以），不要自行换算单位。
- 用户给的是表格、截图文字或口语时，先整理成上面的格式，并把整理结果展示给用户。
- **绝不猜测数值。** 用户说“功率调高一点”这类模糊要求时，追问具体值。
- 不确定参数名时，先执行 `list` 查看支持的参数和当前值。

## 流程

1. **整理参数**，写入临时文件（例如 `/tmp/bt-rf-<消息ID>.txt`），不要把参数直接拼进命令行。

2. **校验（必做，很快）**：
   ```bash
   python3 {baseDir}/scripts/jenkins_rf.py run --action check --params-file /tmp/bt-rf-<消息ID>.txt --requester "<用户姓名>" --request-id "<消息ID>-check"
   ```
   - 失败：把输出中的“参数错误”原样告诉用户，请其修正，不要擅自改值重试。
   - 通过：输出里有每个参数的“旧值 → 新值”。如果参数是你根据表格或口语整理/推断出来的，先把这份变更给用户确认；用户原话已经给出明确参数并要求编译时，可以直接进入下一步。

3. **编译（耗时较长，作为后台长任务运行）**：
   ```bash
   python3 {baseDir}/scripts/jenkins_rf.py run --action build --params-file /tmp/bt-rf-<消息ID>.txt --requester "<用户姓名>" --request-id "<消息ID>"
   ```
   - 启动后立即回复用户：已提交编译，完成后会通知。
   - `--request-id` 用触发本次请求的飞书消息 ID：同一 ID 重复执行不会重复编译，而是继续跟踪原构建。
     拿不到消息 ID 时，自己生成一个唯一 ID（如 `<用户>-<时间戳>`），并在本次请求的后续重试中沿用同一个 ID。
   - 脚本会一直等到构建结束（包括排队），进度写在 stderr，最终报告在 stdout。
   - 结束后把 stdout 的报告发到用户提交请求的那个飞书会话（可以调整排版，但不要删改参数、错误摘要和链接）。
   - 如果无法以后台长任务方式等待：改用 `--no-wait`，它会在构建开始后返回 `build_url`；把构建地址告诉用户，之后定期用 `status --build-url <URL>` 查询，结束后回复结果。
   - 退出码：0 成功；1 编译失败或被取消；2 配置/用法错误（告诉用户需要管理员处理）；3 等待超时（构建可能仍在进行，用下面的 `wait` 继续等待）。

4. 用完删除临时参数文件。

## 其它操作

| 用户需求 | 命令 |
|---|---|
| 支持哪些参数 / 当前值 | `run --action list` |
| 查询某次编译 | `status --build-url <URL>` |
| 继续等待结果 | `wait --build-url <URL>` |
| 按消息找回构建 | `find --request-id <消息ID>` |
| 取消编译 | `cancel --build-url <URL>`（只取消该用户自己提交的构建） |

多项目时加 `--project <项目名>`；用户没说明且 `list` 报错要求指定项目时，询问用户。

## 注意

- 同一时间只有一个编译在执行，其余在 Jenkins 排队；排队是正常的，告诉用户耐心等待即可。
- 编译成功后提醒用户：NVRAM 默认值只在 NVRAM 为空时生效，刷机需选择 Format All + Download 或清除 NVRAM。
- 不要输出、记录或转述 `JENKINS_TOKEN` 等凭证；不要调用本技能以外的 Jenkins 接口。
- 报告中出现“源码还原异常”时，提醒用户联系管理员。
