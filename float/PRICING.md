# 计量与参考价格

Last updated: `2026-09-30T22:27:07+08:00`

## 默认自动额度对比（0.4.0）

每个任务按已计量请求分别计算实际模型的 Standard credits，以及相同输入/缓存/输出结构全部使用 Astra 的参考 credits。主任务聚合可见子孙代理，按线程 ID 去重，继承历史已在请求计量时排除。

- 参考额度差额 = Astra 基线 credits − 实际模型参考 credits。
- 比例 = 参考额度差额 / Astra 基线 credits。
- Astra 等价 token = floor(已定价片段 token × 比例)。

这些值由本地算术自动产生，与手动试算参数无关。便宜模型通常为正值，全 Astra 为零，较贵模型保留负值。无可定价记录时显示待计量；未知模型片段排除在对比外并标记部分计量，不当作免费。它比较同量 token 的参考成本，不估计 Astra 完成相同任务实际会消耗多少 token，不直接表示订阅额度扣减或任务质量。

新增模型价格已实际核对：[GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[Codex Standard credits](https://learn.chatgpt.com/docs/pricing#token-rates)。该模型 API >272K 输入的请求有长上下文倍率；本界面保持明确的 Standard 短上下文参考口径。API 支持 low/medium/high/xhigh/max；本机 Codex 目录另公开 ultra，自动路由不使用 6.1 Sol ultra。

## 数据与计算

- 累计 token 来自任务元数据；片段用量来自当前日志的 `token_usage_record.payload.usage`。按 `thread_id` 归属和 `response_id` 去重，排除复制的父任务历史，不累加 `token_count` 的累计值。
- 输入包含缓存输入与缓存写入；输出包含推理输出。因此总数为输入＋输出，不再次加缓存或推理。
- USD 参考值 = 普通输入×输入价＋缓存命中×缓存价＋缓存写入×写入价＋输出×输出价。除以一百万。Credits 使用 Codex Standard 费率，不额外收缓存写入费。
- “同量全 Astra 参考差额”按相同输入/缓存/输出重新计算 Astra 价格后相减。它是价格对照，未估计 Astra 实际需要多少 token，也不代表质量相同。
- “情景预测少用 token”＝已计量输入×用户设定的缩减比例－用户设定的额外委派 token。默认两项均为零；负值表示增加。它是可调整假设的试算，尚无同任务对照实验用于校准。
- 本进程只增量读元数据指向的日志片段，每次最多 2 MiB，不递归扫描全部历史。数字账本持久保存去重键和读位置，重启继续计量；日志轮换不重复累计。去重键由SQLite管理，不再受4096请求内存上限限制；账本不可用时退回有上限的内存模式并标明覆盖不足。写入失败会回滚读位置，恢复后继续。
- 未读完、缺失模型、未知价格和异常记录均显示覆盖状态。缺失数据用“暂无”表达，费用不会由任务总 token 猜算。

## 2026-09-30 官方价目快照

单位为每百万 token。USD 为 API Standard 短上下文参考价，credits 为 Codex Standard 参考费率。

| 模型 | 输入 USD | 缓存 USD | 写入 USD | 输出 USD | 输入/缓存/输出 credits |
|---|---:|---:|---:|---:|---|
| GPT-6 Astra | 10 | 1 | 12.5 | 50 | 250 / 25 / 1250 |
| GPT-6.1 Sol | 2 | 0.1 | 2.5 | 10 | 50 / 2.5 / 250 |
| GPT-6 Sol | 2 | 0.2 | 2.5 | 10 | 50 / 5 / 250 |
| GPT-6 Luna | 0.1 | 0.01 | 0.125 | 0.5 | 2.5 / 0.25 / 12.5 |
| GPT-5.6 Sol | 4 | 0.4 | 5 | 20 | 100 / 10 / 500 |
| GPT-5.6 Terra | 2 | 0.2 | 2.5 | 12 | 50 / 5 / 300 |
| GPT-5.6 Luna | 0.2 | 0.02 | 0.25 | 1.2 | 5 / 0.5 / 30 |

已实际读取：[API Pricing](https://developers.openai.com/api/docs/pricing)、[Codex Pricing](https://learn.chatgpt.com/docs/pricing#token-rates)。该表为内置种子；运行中从 router 的本地校验快照读取更新，Float 不联网。router 公开目录刷新、价格采用和本地准入策略见其 docs/model-registry.md；首次计量的快照 hash 存入数字账本，后续更新不追溯改价。界面明确显示逐请求冻结口径。

订阅额度由官方用量面板确定，不能从 API 美元或 credits 直接换算剩余额度。Fast mode、长上下文、工具、多模态、地区及合约可能有不同收费；本界面的 Standard 参考值不计这些差价。本地记录不足以逐请求验证速度，因此只按 Standard 参考费率比较。

## 手动数量试算（0.3.0 引入，0.4.0 保留）

卡片右侧的累计 token 使用官方任务元数据。预计节省使用当前计量片段：`floor(inputTokens × 输入缩减百分比 / 100) − 委派额外 tokens`；比例为这个差值除以同一片段的 `totalTokens`。无有效片段时显示待计量，负值显示预计多用。它是本地参数试算，默认参数为零；这个试算与额度折算独立，也不代表路由的实测节省。

## 0.5.0 持久计量与本轮显示

本轮数字按 `turn_id` 聚合已计量请求；当前无usage时显示待计量，完整度随增量读取更新。累计token继续采用Codex任务元数据。额度节省保持“同量全Astra”的参考比较，不能用它代替benchmark中的同任务实测差值。USD未知与credits未知分别处理；没有可信credits的请求不会进入额度对比，也不会被当作零价。
