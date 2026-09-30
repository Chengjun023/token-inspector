# 可更新模型目录

Last updated: `2026-09-30T22:40:46+08:00`

目录发现、模型准入和宿主执行能力分别管理。普通路由 CLI 的 `propose` 调用
`load_snapshot(home, refresh=True)`，按 24 小时 TTL 刷新公开元数据；首次没有
外部缓存时也尝试刷新。失败冷却为 15 分钟。Float 和冻结 benchmark 使用
`load_snapshot(..., refresh=False)` 或 `read_snapshot(path)`，始终离线。
`load_snapshot` 默认离线且不写文件。

## 数据结构

顶层字段为 `schema="adaptive-router.model-registry"`、`schema_version=1`、
`version`、`hash="sha256:..."`、`source`、`fetched_at` 和 `models[]`。
hash 使用排序后的 UTF-8 JSON 内容计算，排除 hash 本身及瞬时的
`cache_status/stale/error/cache_path/last_attempt_at`。

模型身份由 `provider/id` 组成，例如 `openai/gpt-6.1-sol`。执行端点另存为
`endpoints[]`，每项含 `provider/id/kind/verified`；例如模型对应的 native
端点为 `codex/gpt-6.1-sol`。公开源列出的 API 端点不会被转换为 native。

| 字段 | 含义 |
| --- | --- |
| `status` | `verified`、`candidate`、`admitted` 或 `disabled` |
| `capabilities` | `verified`、`efforts[]`、`tools`、`reasoning`、`context_window`、输入/输出模态 |
| `pricing.usd_per_million` | `input/cached_input/cache_write_input/output`，未知为 null |
| `pricing.credits_per_million` | `input/cached_input/output`；公共 feed 没有 Codex credits，保持未知 |
| `pricing.as_of/source/verified/basis` | 费率日期、来源、是否经核对及适用口径 |
| `routing` | `tier/strengths/latency_ms/latency_verified/verified/safety_equivalent_verified/source` |
| `admission` | 本地声明、证据及更新时间；不由公共 feed 自动生成 |

`tools` 为 bool 或 null，表示是否声明支持工具调用；具体命名工具仍需宿主证据。
`strengths` 的分数范围为 0–1。延迟、上下文大小和 workload 评分没有证据时为
null 或空表。内置 Astra/Sol/Terra/Luna tier 来自既有路由启发式，
`routing.verified=false`；只有 Astra 的现有安全门禁锚点设为
`safety_equivalent_verified=true`，不据此声称跨模型质量等价。

内置七个 GPT-6.1/6/5.6 型号的 USD/credits 费率与 2026-09-30 核实记录一致。
USD 采用 API Standard 短上下文参考口径，credits 采用 Codex Standard 参考口径；
速度、长上下文、工具差价及实际订阅账单不在该表范围内。

## 更新、缓存和失败恢复

默认源为 `https://models.dev/api.json`。只下载 JSON，不读取凭据，不调用模型，
不执行远端代码或 feed 的 `npm` 等字段。HTTPS URL 不能包含凭据、query 或 fragment，
重定向也必须满足同样约束。输入最多 8 MiB、512 providers、16,377 外部模型；
规范化缓存与冻结快照最多 32 MiB，价格嵌套最多 16 层、tier 数组最多 64 项。

价格必须有限且非负。单条无效名称、能力、上下文或负价哨兵记录被隔离，
`source.skipped_models` 和最多 50 条 `source.warnings` 明确记录原因；未知价格为 null，
不会转成免费。JSON 重复字段、NaN/Infinity、越界大小、无模型或无有效行使
整次更新失败。tier 价格结构会校验，但实际参考计价只使用 feed 的基础费率，
不自动推断上下文分段账单。

默认目录位于 `~/.codex/adaptive-router/model-registry/`，也可用
`ADAPTIVE_ROUTER_HOME` 或 API 的 `home` 参数指定根目录。

- `lastgood.json`：通过验证的外部目录，原子替换。
- `snapshots/<sha256hex>.json`：不可覆盖的历史目录和本地准入/计价版本。
- `policy.json`：本地准入、禁用、显式价格采用和宿主能力扩展证据。
- `refresh-status.json`：最近刷新失败及冷却时间；不改变 lastgood 内容。

失败返回原目录并明确 `stale=true/error/cache_status=refresh_failed`；冷却期间返回
`cache_status=cooldown`。缓存损坏会明确返回 `cache_invalid` 并使用内置目录。
全新 feed 可以更新候选发现，已知型号的新公开价先存入 `public_metadata.pricing`，
不会静默覆盖核实费率。`adopt-pricing` 是本地显式采用，无需修改插件源码或 Git；
采用后缺失的 Codex credits 保持未知。更新后的目录不能修改旧冻结快照或旧请求费用。

## API 与本地准入

```python
from model_registry import (
    bundled_snapshot, load_snapshot, read_snapshot, get_model,
    eligible_models, freeze_snapshot, price_usage,
    admit_model, admit_host, host_models, adopt_pricing,
)

snapshot = load_snapshot(home="/tmp/router-demo", refresh=False)
model = get_model(snapshot, "gpt-6.1-sol", provider="openai")
usable = eligible_models(snapshot, {"gpt-6.1-sol": ["medium", "high"]})
freeze_snapshot(snapshot, "/tmp/frozen-models.json")
price = price_usage(snapshot, "gpt-6.1-sol", {
    "input_tokens": 1000, "cached_input_tokens": 200,
    "cache_write_input_tokens": 0, "output_tokens": 100,
})
```

`price_usage` 返回 `usd/credits/known/credits_known/priceVersion/snapshot_hash`
及模型身份和 `price_verified`。每次请求应记录所用 hash 和计算结果，不能在
刷新目录后用新价格重算历史请求。缺失所需价格返回 null；cached/write 输入必须
是 input 的子集。冻结 benchmark 直接读 `read_snapshot`，不会跟随实时目录变化。

新型号无需改代码即可进入 candidate；显式 `admit_model` 接受 endpoint、efforts、
证据及可选的 `capabilities/routing` 声明。`verified`、tier 和安全等价标记必须有
本地核对证据，公共元数据不能替用户完成这一步。`admit_host` 单独记录宿主扩展；
`host_models` 只返回该扩展，不替代路由现有 baseline。最后仍需当前工具实际提供
的 endpoint 和 efforts 交集；从目录发现型号或手动准入都不等于宿主已可执行。

```sh
python3 scripts/model_registry.py --home /tmp/router-demo import data/models.dev.shape.fixture.json
python3 scripts/model_registry.py --home /tmp/router-demo show
python3 scripts/model_registry.py --home /tmp/router-demo refresh
python3 scripts/model_registry.py --home /tmp/router-demo freeze /tmp/frozen-models.json
python3 scripts/model_registry.py --home /tmp/router-demo admit valid-tiered --provider synthetic-provider --endpoint-provider codex --endpoint-id future-native --endpoint-kind native --efforts medium,high --tools --capabilities-verified --tier standard --routing-verified --evidence '已核对实际工具目录与本地验收记录'
python3 scripts/model_registry.py --home /tmp/router-demo admit-host future-native --efforts high --evidence '已核对宿主spawn工具支持此型号与high'
python3 scripts/model_registry.py --home /tmp/router-demo disable valid-tiered --provider synthetic-provider --evidence '用户本地禁用'
python3 scripts/model_registry.py --home /tmp/router-demo adopt-pricing gpt-6.1-sol --verified --evidence '已核对公开来源与Standard短上下文口径'
```

最后一条仅在该型号已有待采用的 `public_metadata.pricing` 时有效。
`refresh --force` 显式跳过 TTL 和失败冷却。刷新失败 CLI 返回码 1，并输出完整可用
fallback；无效本地输入返回码 2。

## 验证记录

- `2026-09-30T22:36:05+08:00`：真实公开源刷新成功，8,321 个目录条目，含 8,314 个
  candidate；4 条无效名称行被隔离。规范化快照约 10.36 MB，未调用任何模型 API。
  元数据证据见 `data/public-refresh-check.json`；未在插件内保存原始 feed。
- `2026-09-30T22:40:46+08:00`：30 项单测覆盖 TTL/冷却、失败保留、hash/原子历史、
  坏行隔离、实际 tier 形状、候选准入与宿主/efforts 交集、价格采用及历史计价不变。
  全插件 129 项回归通过。`data/models.dev.shape.fixture.json` 是合成 fixture，不代表
  真实型号或质量评测。
