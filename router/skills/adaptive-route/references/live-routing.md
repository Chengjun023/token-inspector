# 实验性同轮设置适配器

适用条件：用户要切换当前主任务，本机已经有可连接的 Codex app-server control socket。脚本为 `../../../scripts/live.py`（相对此文件目录），不启动服务。

先运行 `python3 LIVE probe`，可用 `--socket /absolute/path.sock` 指向用户已配置的 daemon。默认通过 `codex app-server proxy` 连接官方默认 control socket。只读取当前 thread 的摘要及最后一个 turn 的无内容元数据。不会读对话正文。

如果 probe 返回当前 thread 的 `active` 状态和 `inProgress` turn，用刚得到的 turn ID 和已选择的路由 ID：

```sh
python3 LIVE apply --id ROUTE_ID --turn-id TURN_ID
```

适配器再次验证 thread 与 turn，调用 `turn/settings/update`，仅传 threadId、turnId、model、effort。不会改权限、审批、service tier、其他任务或未来轮次。RPC 写入之前预留路由，防止未知结果自动重试；发生 timeout 必须报告结果未知，不立即改派同一工作。

- `applied`：设置已发布给后续捕获的步骤；已捕获的模型请求保持原样，也可能不再有后续推理。
- `targetUnavailable`：指定活跃目标已结束，没有改为切换新轮次。
- 失败或接口不存在：不能声称主任务已经切换。恢复原生路由时另建决策，并确认此前没有未知写入结果。

当前已检查的 CLI 为 `0.155.0-alpha.9.2`。`turn/settings/update` 的上述语义来自本机 `codex app-server generate-json-schema --experimental`；公开 App Server 页面没有说明这一实验方法。此电脑的桌面当前不开放默认 control socket，因此没有完成该接口的真实切换验收。不要把为脚本测试创建的独立 app-server 当作桌面当前任务。

官方通用接口参考：https://learn.chatgpt.com/docs/app-server
