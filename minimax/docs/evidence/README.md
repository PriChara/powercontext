# ORD-17 验证记录

| 步骤 | 实测结果 | 记录 |
| --- | --- | --- |
| 导入与检索 | seekdb 从规格、代码和测试中取回任务相关资料，首次查询没有任务检查点 | [导入](ingest.json)、[初始查询](empty_context.json) |
| 基线 | 新功能断言失败，退出码为 1。3 项回归通过，退出码为 0 | [基线测试和 PowerContext 写入](baseline.json) |
| 跨进程恢复与规格更新 | 新进程从 PowerContext 读回基线状态。把规格中的 9 位上限改成 6 位，重新导入后检索命中新版，随后恢复原规格 | [基线恢复](restarted_context.json)、[更新后检索](updated_spec_context.json) |
| MiniMax 修改 | MiniMax Code 网页版使用 M3.1-Flash 根据检索资料和恢复状态生成完整的 `orders.py` | [输入全文](minimax-prompt.txt)、[开发记录](minimax-call.json)、[候选代码](orders.py)、[差异](orders.patch) |
| 候选验证 | 原测试文件摘要不变，4 项功能测试和 3 项回归全部通过，退出码为 0 | [候选测试](after-tests.json) |
| 最终状态 | 新进程读回 PowerContext 的第二个检查点，包含候选及测试摘要、通过状态和引用 | [最终检查点](verified-context.json) |

MiniMax Code 答复中的 Python 部分保存在 [答复代码](minimax-answer.txt)，与参赛者从网页下载的 `orders.py` 核对一致。模型名称由参赛者根据页面确认。网页答复的文件资源标识是 `commit-id-462531412590792`，网页版接入没有采集 API 请求 ID 或 token 用量。答复解释了为何区分字段缺省和显式 `None`，为何校验 ASCII 数字，以及为何在遍历订单前校验输入。
