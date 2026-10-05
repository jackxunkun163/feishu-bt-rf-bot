# MTK 蓝牙射频参数编译（小龙虾 + Jenkins）

在飞书里对小龙虾（OpenClaw）说要改哪些蓝牙射频（BT RF）参数，就会自动完成：
校验参数 → 同步源码 → 修改参数 → 编译 → 还原源码 → 把结果（改动、diff、产物或错误摘要）回复给提交人。

```
飞书用户 ──> 小龙虾（整理参数）──Jenkins API──> 参数化 Pipeline（编译节点）
                   ^                               │ 校验 → 同步 → btbot.rf apply → 编译 → restore
                   └──── 后台长任务轮询结果 ─────────┘ 结果写入构建产物 rf-out/summary.json
```

| 目录 / 文件 | 说明 | 运行位置 |
|---|---|---|
| `openclaw/mtk-bt-rf/` | 小龙虾技能：整理参数、调用 Jenkins、回复结果（只依赖 Python 标准库） | 小龙虾所在机器 |
| `jenkins/Jenkinsfile` | 参数化 Pipeline | Jenkins |
| `btbot/` | `python -m btbot.rf`：校验并修改参数、备份与还原（只依赖 PyYAML） | Jenkins 编译节点 |
| `rules.example.yaml` | 参数规则示例：哪些参数能改、在哪个文件、取值范围 | 编译节点（复制为 rules.yaml） |

参数格式（每行一个；小龙虾会把自然语言、表格整理成这个格式）：
```
Radio[0]=0x07                  # 数组的某个元素
TxPWOffset=0x80,0x82,0x80      # 一次给出数组全部元素
BtTxPower=9                    # 单个值
```

## 1. 参数规则

把 `rules.example.yaml` 复制到编译节点，保存为 `rules.yaml`，然后按实际工程修改。这份文件包含公司内部的源码路径，不要提交到公开仓库。

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

可以在编译节点上直接验证规则是否写对：
```bash
python3 -m btbot.rf list --rules rules.yaml --root /home/build/alps
```

## 2. Jenkins

1. **新建 Pipeline 任务**，例如命名为 `bt-rf-build`：
   - 选择 Pipeline script from SCM，指向本仓库。Jenkins 访问不了 GitHub 时，可以指向公司内网的镜像仓库。
   - Script Path 填 `jenkins/Jenkinsfile`。
2. **修改 `jenkins/Jenkinsfile` 顶部的 `CFG`**，修改后提交到你们自己的仓库或镜像：
   - 编译节点标签、源码目录、`rules.yaml` 路径
   - 同步命令、编译命令（从现有编译任务里复制）
   - 产物共享目录（可选）
3. **首次运行**：点「立即构建」跑一次，默认 ACTION=list。之后 Jenkins 才会识别参数，并列出参数和当前值。
4. **给小龙虾建一个专用 Jenkins 账号**：只授予该任务的 Read、Build、Cancel 权限，然后生成 API Token。

编译节点需要有 `python3` 和 `python3-venv`；首次运行时会在工作区创建虚拟环境，并安装 PyYAML。

| ACTION | 作用 | 耗时 |
|---|---|---|
| `list` | 列出参数及当前值 | 几秒 |
| `check` | 校验参数、预览 diff，不修改源码 | 几秒 |
| `build` | 先校验 → 同步源码 → 修改 → 编译 → 无论成败都还原源码 | 取决于编译 |

## 3. 小龙虾技能

1. 把 `openclaw/mtk-bt-rf/` 整个目录复制到小龙虾的技能目录。例如：
   - 全局技能：`~/.openclaw/skills/mtk-bt-rf/`
   - 工作区技能：`<workspace>/skills/mtk-bt-rf/`
2. 为技能提供环境变量：
   - `JENKINS_URL`
   - `JENKINS_USER`
   - `JENKINS_TOKEN`
   - `JENKINS_JOB`：例如 `bt-rf-build`，在文件夹里的写 `文件夹/bt-rf-build`
   - `JENKINS_INSECURE=1`：可选，Jenkins 使用自签名证书时设置

   可以在 OpenClaw 配置的 `skills.entries.mtk-bt-rf.env` 中设置，也可以设置在运行小龙虾的环境里。具体写法以你所用 OpenClaw 版本的文档为准。如果小龙虾在沙箱（Docker）中执行命令，要确保沙箱里也有这些变量，并且能访问 Jenkins。
3. 在小龙虾所在机器上验证：
   ```bash
   python3 ~/.openclaw/skills/mtk-bt-rf/scripts/jenkins_rf.py run --action list
   ```
4. 在飞书里对小龙虾说「列出蓝牙射频参数」，确认整条链路是通的。之后就可以直接说「把 Radio[0] 改成 0x07 编译一下」，它会按 `SKILL.md` 的流程执行：校验 → 编译（作为后台长任务）→ 回复结果。

## 健壮性

| 场景 | 处理方式 |
|---|---|
| 参数名写错、越界、`nan` 等非法值，或同一参数给了两个不同的值 | 校验阶段几秒内就失败（不用等同步源码），把错误逐条回复给用户，**不修改任何文件** |
| 规则定位不唯一，或 anchor 后面不是数组 | 拒绝修改，避免改错位置 |
| 修改文件 | 原子写；GBK 注释、CRLF 换行原样保留 |
| 编译失败、超时、被取消 | Pipeline 收尾步骤无论成败都还原源码；从编译日志中提取错误摘要回复给用户 |
| 编译期间有人改了同一个文件 | 不覆盖对方的修改，构建标红，报告中提示联系管理员 |
| 多人同时提交 | Jenkins 串行执行，其余排队 |
| 同一条消息被重复处理 | 用飞书消息 ID 作为请求 ID，不会重复编译，而是继续跟踪原来的构建 |
| 触发请求的响应丢失 | 先按请求 ID 确认构建是否已提交，再决定是否重试 |
| 网络抖动、Jenkins 重启 | 自动重试，并继续等待构建结果 |
| Jenkins 开启了 CSRF crumb | 自动获取后提交 |
| 节点离线等导致没有 summary.json | 改用控制台日志末尾作为错误信息 |
| 参数注入 | 参数通过文件交给 btbot.rf，不经过 shell；项目名和请求 ID 按白名单字符校验 |

## 注意
- NVRAM 默认值（`CFG_BT_Default.h`）只在 NVRAM 为空时生效，刷机时需要清除 NVRAM 或 Format All。
- 每次构建的 `rf-out/summary.json` 和 `rf-out/changes.diff` 都会归档到 Jenkins，便于追溯。

## 测试与 CI
```bash
pip install -r requirements.txt pytest
python -m pytest tests
```
GitHub Actions（`.github/workflows/ci.yml`）在 push 到 main 和提交 PR 时自动运行全部测试，覆盖 Ubuntu（Python 3.9、3.12）和 Windows。测试内容包括：
- 参数解析、校验与文件修改
- `btbot.rf` 命令行
- 用模拟 Jenkins 服务器测试技能脚本：排队、防重复、CSRF、失败报告、取消等

修改源码和编译只在 Jenkins 编译节点上进行，CI 不需要 Android 源码。
