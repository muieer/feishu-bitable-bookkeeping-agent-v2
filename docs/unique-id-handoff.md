# unique_id 核对与单次补写：开发交接报告

## 验收对象

开发目录：`/Users/yanghuhu/.codex/worktrees/master-worktree/feishu-bitable-bookkeeping-agent-v2`。基准提交为 `7b91544`，当前分支为 `master`，本次实现为该目录中的未提交改动。原项目目录仍在 `learn` 分支，不能用原目录的源码代替验收对象。

项目规则见根目录 `AGENTS.md`。验收范围以用户给出的原始业务要求为准；本报告用于说明实际实现和提供定位信息，不能作为正确性的独立证据。公开业务说明见 `README.md`、`docs/architecture.md` 和 `src/bookkeeping/prompts.py`。未纳入版本控制的个人 `knowledges/` 没有复制到本 worktree，开发与验收均使用人工合成资料。

## 原始目标与实际交付

目标表已有文本字段 `unique_id`。本次交付的流程是：首次提交结果不确定时，保存待核对状态，异步等待 3 秒，按同一个 `unique_id` 精确查询一次；唯一记录且内容一致时确认成功，只有完整空结果才补写一次。

程序在每笔新消费通过现有业务校验后生成 UUIDv4，在首次远端提交前，将 UUID、原文范围和 `Expense` 快照一起提交到现有 SQLite 执行日志。首次写入和补写均从已保存的快照取数据，并使用同一个 UUID；模型工具参数中没有 ID 字段。补写不调用模型、不重新校验或计算日期及分类。

首次写入的明确成功和明确失败沿用现有回执分类。核对时使用原有 CLI、原身份及同一账本，不限定视图；过滤条件为 `unique_id == UUID`。返回必须是正常退出的成功 JSON 信封，字段矩阵和行 ID 数组有效，且 `has_more` 明确为 `false`。完整空结果允许单次补写；唯一匹配还需核对日期、金额、详情和全部分类，再保存远端行 ID 并确认成功。日期按配置时区的日历日比较，金额按数值比较；无三级分类接受 `null` 或空数组。

查询失败、超时、格式无法解析、结果不完整、内容冲突或多条匹配时保留待核对，不补写。补写仅明确成功才更新为成功；其他结果均保留待核对，补写被明确拒绝也不能把首次提交认定为失败。没有第二轮等待、查询或补写。

同一消息的重叠原文范围再次进入工具时，优先返回已有提交记录，不因后续提议更改分类或日期而重启流程。不同原文出现位置和不同消息仍沿用现有的独立消费处理方式。

## 主要改动与兼容处理

| 位置 | 实际改动 |
|---|---|
| `src/bookkeeping/journal.py`：`Journal.__init__`、`reserve`、`existing_write`、`submission` | `writes` 增加可空 `unique_id`；预留时生成 UUIDv4 并保存校验后的消费；提供已有记录与持久快照读取。 |
| `src/bookkeeping/tools.py`：`_create`、`_submit`、`add_expenses` | 在新增工具内部完成一次等待、查询和可选补写；先保存待核对，补写未确认统一保留待核对；重复原文优先使用已有记录。 |
| `src/bookkeeping/cli.py`：`Ledger`、`fields`、`create`、`find_write`、`_matches`、`check_schema` | 内部账本写入接口接收程序 ID；构造带 ID 的写入及精确查询；保守解析完整结果并比较业务内容；启动验证 `unique_id` 类型。 |
| `tests/test_reconciliation.py` | 新增 50 项离线开发检查，运行实际 Agent、工具、SQLite 和 CLI 适配逻辑。 |
| `tests/support.py`、`tests/test_cli.py`、`tests/test_harness.py`、`tests/test_reliability.py` | 适配内部账本接口中的 ID 参数；保持既有测试的业务判断。 |
| `README.md`、`docs/architecture.md`、`docs/engineering-safeguards.md` | 更新字段约束、单次核对补写与恢复边界说明。 |

SQLite 迁移仅增加可空列，旧行保持 `NULL`，不生成 ID、不自动重写；旧报告结构不变。执行日志的命名空间隔离、原文范围主键、同步持久保存方式保持不变。没有新增存储系统或后台任务。

取消或任务总超时可中断首次写入、等待、查询和补写；新增工具保存待核对后继续传播取消，服务按原有方式保存中断报告。进程中断仍由现有恢复逻辑处理，不在重启后自动查询或补写。Harness、服务收尾和通知补发逻辑没有新增写入职责，模型仍只有原来的两个账本工具。最终报告从 SQLite 条目生成，卡片补发读取已保存报告。

## 开发自测结果

2026-10-19 在上述 worktree 完成以下离线检查，结果记录见 `docs/verification/unique-id-development.txt`：

- 全套公开测试：`177 passed, 2 skipped`。本次新增文件包含 50 项参数展开后的检查；其余为既有回归测试。
- 静态检查：`ruff check --no-cache src tests scripts` 通过。
- 差异检查：`git diff --check` 通过。

开发验证覆盖实际业务执行链路中的明确回执、不确定回执、原提交已落库与未落库、核对异常及业务冲突、补写未确认、重复进入、独立消费 UUID、取消和重新打开日志、旧库迁移、字段检查及报告与卡片状态。持久保存时序使用第二个 SQLite 连接检查。模拟账本不对 `unique_id` 提供唯一约束，并验证了原请求延迟落库后与补写共存的已接受边界。

开发测试使用本地脚本模型和模拟 CLI 边界，未调用真实模型、飞书或通知渠道。新增测试主动阻断网络连接和真实子进程启动。可控等待保留异步让出，核对生产代码请求的等待参数为 3 秒。既有依赖出现 2 项弃用警告，未影响测试结果。

复现开发检查时，在验收目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/yanghuhu/ai_project/feishu-bitable-bookkeeping-agent-v2/.venv/bin/python -m pytest -q -p no:cacheprovider
/Users/yanghuhu/ai_project/feishu-bitable-bookkeeping-agent-v2/.venv/bin/ruff check --no-cache src tests scripts
git diff --check
```

共享原目录虚拟环境只提供解释器和已安装依赖；pytest 的 `pythonpath=src` 使用本 worktree 的源码。未运行真实环境 `doctor` 或真实模型测试脚本。

## 限制、未验证事项与需求偏离

- 原请求可能在空查询之后延迟完成，与补写形成同 ID 的重复记录。这是用户明确接受的边界；本次没有实现严格幂等、远端唯一约束、第二轮核对或自动消重。
- 取消、总任务超时或进程退出可能在 3 秒等待或后续操作结束前中断流程，届时保留待核对，重启后不补写，沿用原有中断约束。
- 未验证真实飞书接口的响应、真实目标字段现状、可见性延迟、真实模型理解准确率或真实卡片送达能力。CLI 精确文本过滤与字段输出依据本地 CLI 帮助、随附 skill 和现有项目契约；真实服务仍需另行验证，本次按要求全程离线。
- 私有知识库和真实模型回放资料缺失，对应 2 项测试跳过。未读取个人配置、生产执行日志或私有消费数据。
- 既有文档记录的「模型同轮写入与结束声明使用写入前状态校验」边界仍存在，详见 `docs/architecture.md` 的 LangChain 运行边界；本次没有调整这项既有行为。

本次实际交付与本次用户业务要求没有已知偏离。独立验收尚未完成，本报告不声明验收通过，也不规定验收场景、用例数量或预期结论。
