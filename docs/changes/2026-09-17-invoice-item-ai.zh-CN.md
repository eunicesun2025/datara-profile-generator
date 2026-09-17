# 发票明细 item 按 AI 字段处理

用户确认 `AI_Invoice_Detail.item` 不是 System 字段。模型依据提示词按明细数组顺序返回字符串行号（"1"、"2"……），属于预期输出。

- 去掉导入、Profile 校验、AI 草稿建议中针对 item 的硬编码 System 处理。导入保留 Mapping 指定的 Source；仅 Source=AI 的字段进入提取结构。
- 发票示例将 item 设置为 AI/String，明确写入从 1 开始递增的规则。
- 生成提示词允许按明确的 AI 字段规则生成行号，避免统一禁止行号与该字段规则冲突。
- item 与其他 AI 字段一样参与缺少键、类型和输出结构检查。此改动不额外引入顺序编号的强制校验，也不放开 file_id/head_id 等系统字段。
- 当前本地 VendorInvoiceHK_PRD 已通过保存接口将 item 改为 AI，保留提示词版本记录。历史结果不改写，重新运行测试产生新校验结果。其他 Profile 不批量迁移。
- current_date 与其他字段的来源不在本次修改范围内。

需要重启原有服务以加载更新的后端规则，并重新打开 Profile，避免浏览器旧草稿覆盖新修订。
