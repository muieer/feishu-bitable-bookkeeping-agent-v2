# 测试与证据管理

公开测试使用人工合成知识库、虚构表 ID、模拟模型和隔离账本，不需要个人凭证，也不会联网调用模型或飞书。分类清单与规则由测试夹具在临时目录生成，避免把个人知识库复制到公开测试文件中。

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests scripts
```

真实模型回放从 `knowledges/testing/real_model_classification.json` 和本地个人知识库读取。缺少私有资料时，对应回放测试自动跳过；其他测试仍可运行。

初次实现已通过 73 项本地测试，并验证真实表字段读取、长连接握手和 CLI 新增格式预览。该轮真实模型测试累计发出 10 次请求，4 组完整通过，最后 1 组在总请求上限处未完成模型收尾。修复后的私有模型参数回放通过；回放的结束信号由测试模拟，不能替代完整联网验证。真实表新增及卡片送达尚未做端到端实测。

包含个人分类路径、规则和模型输出的详细测试报告保存在 `knowledges/testing/test_report.md`。实际请求计数、日志、场景报告和执行数据库保存在 `.runtime/live-tests/`。这两个目录均被 Git 忽略，仅在本地保留。

需要真实模型测试时，先准备本地配置、个人知识库及 `knowledges/testing/live_cases.json`，再运行：

```bash
.venv/bin/python scripts/live_test.py --run-dir .runtime/live-tests/a-new-run
```

每个目录独立持久计数，最多发出 10 次模型请求，最终校验要求数量处于 5～10 次范围。复用目录会保留已有计数；`--retry-failed` 只重测失败案例，不能重置预算。已有测试目录用完预算时，不再继续请求模型。
