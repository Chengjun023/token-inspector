import AppKit
import SwiftUI

enum DemoMode {
    static let enabled = ProcessInfo.processInfo.arguments.contains("--demo")
        || Bundle.main.object(forInfoDictionaryKey: "TokenInspectorDemo") as? Bool == true
}

struct Difficulty: Decodable {
    let score: Double?
    let source: String?
    let confidence: String?
    let task_type: String?
    let reasons: [String]?
}
struct Usage: Decodable {
    let inputTokens: Int?; let cachedInputTokens: Int?; let outputTokens: Int?; let reasoningTokens: Int?
    let totalTokens: Int?; let pricedTokens: Int?; let requests: Int?
    let costUSD: Double?; let credits: Double?; let baselineUSD: Double?; let savingUSD: Double?
    let baselineCredits: Double?; let savingCredits: Double?
    let coverage: String?; let source: String?; let priceBasis: String?; let priceDate: String?; let tokenSavingStatus: String?
    let currentTurn: TurnUsage?
    let persistent: Bool?
    let priceSnapshot: String?
}
struct TurnUsage: Decodable {
    let turnId: String
    let totalTokens: Int
    let requests: Int
    let costUSD: Double?
    let partial: Bool
}
struct ReferenceSavings: Decodable {
    let equivalentTokens: Int?
    let percent: Double?
    let savedCredits: Double?
    let savedUSD: Double?
    let pricedTokens: Int
    let totalTokens: Int
    let taskCount: Int
    let missingTaskCount: Int
    let includesChildren: Bool
    let partial: Bool
}
struct Agent: Identifiable, Decodable {
    let id: String
    let parent: String
    let title: String
    let nickname: String
    let model: String
    let effort: String
    let status: String
    let activity: String
    let tool: String
    let cwd: String
    let tokens: Int
    let updatedAt: Double
    let startedAt: Double
    let source: String
    let difficulty: Difficulty?
    let routeModel: String?
    let routeEffort: String?
    let routeStatus: String?
    let routeReason: String?
    let routeDecisionID: String?
    let routeProfile: String?
    let routeOutcome: String?
    let routeRegistry: String?
    let usage: Usage?
    let savings: ReferenceSavings?
    var busy: Bool { status == "running" || status == "waiting" }
    var modelLabel: String { model.replacingOccurrences(of: "gpt-", with: "GPT-").replacingOccurrences(of: "-astra", with: " Astra").replacingOccurrences(of: "-sol", with: " Sol").replacingOccurrences(of: "-luna", with: " Luna").replacingOccurrences(of: "-terra", with: " Terra") }
    var effortLabel: String { ["none":"无", "minimal":"最低", "low":"低", "medium":"中", "high":"高", "xhigh":"极高", "max":"最高", "ultra":"Ultra"][effort] ?? effort }
    var stateLabel: String { ["running":"运行中", "waiting":"待答复", "completed":"已完成", "interrupted":"已中断", "failed":"出错", "stale":"待确认", "unknown":"未知"][status] ?? status }
    var color: Color { ["running":.green, "waiting":.orange, "completed":.secondary, "interrupted":.secondary, "failed":.red, "stale":.orange, "unknown":.secondary][status] ?? .secondary }
    var activityLabel: String {
        ["reasoning":"推理事件", "agentMessage":"回复", "commandExecution":"执行命令", "fileChange":"修改文件", "mcpToolCall":"调用工具", "subAgentActivity":"子代理协作", "contextCompaction":"整理上下文", "userMessage":"用户输入", "tool":"调用工具", "sleep":"等待", "requestUserInput":"请求答复"][activity] ?? (activity.isEmpty ? "无事件记录" : activity)
    }
}
struct Snapshot: Decodable {
    let connected: Bool
    let updatedAt: Double
    let agents: [Agent]
    let message: String
    let warnings: [String]
}

final class MonitorModel: ObservableObject {
    @Published var agents: [Agent] = []
    @Published var connected = false
    @Published var updated = Date.distantPast
    @Published var message = "连接本机 Codex…"
    @Published var warnings: [String] = []
    private var process: Process?
    private var reader: FileHandle?
    private let queue = DispatchQueue(label: "codex.float.metadata")
    private var buffer = Data()

    func start() {
        if DemoMode.enabled {
            guard let url = Bundle.main.url(forResource: "demo-snapshot", withExtension: "json"),
                  let data = try? Data(contentsOf: url),
                  let state = try? JSONDecoder().decode(Snapshot.self, from: data) else {
                message = "Demo fixture missing or invalid"; return
            }
            agents = state.agents; connected = state.connected
            updated = Date(); message = state.message; warnings = state.warnings
            return // Never start the live reader or open a Codex database in demo mode.
        }
        guard process == nil, let script = Bundle.main.url(forResource: "monitor", withExtension: "py", subdirectory: "backend") else { return }
        let p = Process(), pipe = Pipe()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = ["-u", script.path]
        p.standardOutput = pipe; p.standardError = FileHandle.nullDevice
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = env
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.connected = false; self?.message = "监视器已停止，可从菜单重新连接。"
                self?.process = nil
            }
        }
        do {
            try p.run(); process = p
            reader = pipe.fileHandleForReading
            reader?.readabilityHandler = { [weak self] handle in
                let bytes = handle.availableData
                if bytes.isEmpty { handle.readabilityHandler = nil; return }
                self?.queue.async { self?.consume(bytes) }
            }
        } catch { message = "无法启动本机监视器：\(error.localizedDescription)" }
    }
    private func consume(_ bytes: Data) {
        buffer.append(bytes)
        while let end = buffer.firstIndex(of: 10) {
            let line = Data(buffer.prefix(upTo: end)); buffer.removeSubrange(...end)
            guard let state = try? JSONDecoder().decode(Snapshot.self, from: line) else { continue }
            DispatchQueue.main.async {
                self.agents = state.agents; self.connected = state.connected
                self.updated = Date(timeIntervalSince1970: state.updatedAt)
                self.message = state.message; self.warnings = state.warnings
            }
        }
    }
    func stop() {
        // Keep draining stdout until termination; a full pipe must not block shutdown.
        if let p = process, p.isRunning { p.terminate(); p.waitUntilExit() }
        reader?.readabilityHandler = nil
        try? reader?.close(); reader = nil; process = nil
    }
    func descendants(_ root: String) -> [Agent] {
        var result: [Agent] = [], seen: Set<String> = [root]
        func visit(_ parent: String, depth: Int) {
            guard depth < 32 else { return }
            let children = agents.filter { $0.parent == parent }.sorted { a,b in
                a.busy != b.busy ? a.busy : a.updatedAt > b.updatedAt
            }
            for child in children where !seen.contains(child.id) {
                seen.insert(child.id); result.append(child); visit(child.id, depth: depth + 1)
            }
        }
        visit(root, depth: 0); return result
    }
}

struct StatusDot: View {
    let agent: Agent
    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(agent.color).frame(width: 6, height: 6)
            Text(agent.stateLabel).font(.system(size: 10, weight: .medium)).foregroundStyle(agent.color)
        }.fixedSize()
    }
}

struct GlassBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }
    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

struct FloatView: View {
    @ObservedObject var model: MonitorModel
    @AppStorage("detail") private var detail = 1
    @AppStorage("filter") private var filter = "recent"
    @AppStorage("fontSize") private var fontSize = 12.0
    @AppStorage("pinned") private var pinned = true
    @AppStorage("glassOpacity") private var glassOpacity = 0.45
    @AppStorage("inputReduction") private var inputReduction = 0.0
    @AppStorage("delegationOverhead") private var delegationOverhead = 0
    @AppStorage("savingsDisplayMode") private var savingsDisplayMode = "reference"
    @AppStorage("usageDisplayScope") private var usageDisplayScope = "task"
    @State private var settings = false
    @State private var showScenario = false
    @State private var query = ""
    @State private var collapsed: Set<String> = []
    @State private var visibleLimit = 20

    private var roots: [Agent] {
        var result = model.agents.filter { $0.parent.isEmpty }
        func active(_ a: Agent) -> Bool { a.busy || model.descendants(a.id).contains { $0.busy } }
        if filter == "active" { result = result.filter { active($0) || $0.status == "stale" } }
        if !query.isEmpty {
            result = result.filter { a in
                ([a] + model.descendants(a.id)).contains {
                    ($0.title + " " + $0.model + " " + $0.cwd).localizedCaseInsensitiveContains(query)
                }
            }
        }
        result.sort { a, b in
            if active(a) != active(b) { return active(a) }
            if active(a) { return a.id < b.id }
            return a.updatedAt > b.updatedAt
        }
        return filter == "recent" && query.isEmpty ? Array(result.prefix(8)) : result
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 9) {
                Image(systemName: "point.3.connected.trianglepath.dotted")
                    .foregroundStyle(.cyan).font(.system(size: 19, weight: .medium))
                VStack(alignment: .leading, spacing: 2) {
                    Text(DemoMode.enabled ? "Token Inspector · DEMO" : "Codex Float").font(.system(size: 14, weight: .semibold))
                    Text(model.connected
                         ? "\(model.agents.filter { $0.parent.isEmpty && $0.busy }.count) 主任务 · \(model.agents.filter { !$0.parent.isEmpty && $0.busy }.count) 子代理运行"
                         : "等待本机数据")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 2)
                Button { pinned.toggle() } label: { Image(systemName: pinned ? "pin.fill" : "pin") }
                    .help(pinned ? "取消置顶" : "窗口置顶").accessibilityLabel("窗口置顶")
                Button { settings.toggle() } label: { Image(systemName: "slider.horizontal.3") }
                    .help("显示设置").accessibilityLabel("显示设置")
                    .popover(isPresented: $settings, arrowEdge: .trailing) { settingsPopover }
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 14).padding(.top, 11).padding(.bottom, 10)

            VStack(spacing: 7) {
                HStack(spacing: 8) {
                    Picker("内容详细程度", selection: $detail) {
                        Text("简略").tag(0); Text("标准").tag(1); Text("详细").tag(2)
                    }.pickerStyle(.segmented).labelsHidden()
                    Picker("任务范围", selection: $filter) {
                        Text("活跃").tag("active"); Text("最近").tag("recent"); Text("全部").tag("all")
                    }.labelsHidden().frame(width: 76)
                }
                TextField("搜索任务、模型或目录", text: $query)
                    .textFieldStyle(.roundedBorder).font(.system(size: 11))
            }.padding(.horizontal, 12).padding(.bottom, 10)

            Divider()
            ScrollView {
                // Keep ordinary, explicitly batched stacks. Lazy layout and intrinsic sizing
                // previously caused excessive updates while the monitor refreshed.
                VStack(spacing: 8) {
                    if roots.isEmpty {
                        VStack(spacing: 10) {
                            Image(systemName: "rectangle.on.rectangle").font(.title2).foregroundStyle(.secondary)
                            Text(filter == "active" ? "当前没有检测到活动任务" : "暂无匹配任务").font(.headline)
                            Text("在官方 Codex App 中照常开始任务，\n这里会自动更新。")
                                .multilineTextAlignment(.center).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity).padding(.vertical, 35)
                    }
                    ForEach(Array(roots.prefix(visibleLimit))) { root in taskCard(root) }
                    if roots.count > visibleLimit {
                        Button("再显示 20 个任务（共 \(roots.count) 个）") { visibleLimit += 20 }
                            .font(.system(size: 11)).padding(8)
                    }
                }.padding(10)
            }
            Divider()
            HStack(spacing: 7) {
                Circle().fill(model.connected ? Color.teal : Color.orange).frame(width: 5, height: 5)
                Text(model.message).lineLimit(1)
                Spacer(minLength: 2)
                if model.connected { Text(model.updated, style: .time).monospacedDigit() }
                Button { resizeRelative(-40) } label: { Image(systemName: "minus") }
                    .help("缩小窗口").accessibilityLabel("缩小窗口")
                Button { resizeRelative(40) } label: { Image(systemName: "plus") }
                    .help("放大窗口").accessibilityLabel("放大窗口")
            }
            .font(.system(size: 9)).foregroundStyle(.secondary).buttonStyle(.plain)
            .padding(.horizontal, 12).padding(.vertical, 8)
            if detail == 2 {
                Text("模型来自任务设置，状态来自本机轮次记录；每 2 秒刷新。超过 3 分钟无新事件的未结束轮次标为待确认。")
                    .font(.system(size: 9)).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12).padding(.bottom, 8)
            }
            if !model.warnings.isEmpty {
                Text(model.warnings.joined(separator: " "))
                    .font(.system(size: 9)).foregroundStyle(.orange).padding(6)
            }
        }
        .background {
            GlassBackground().opacity(glassOpacity)
                .overlay(Color(nsColor: .windowBackgroundColor).opacity(0.10 * glassOpacity))
        }
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .onChange(of: pinned) { _, value in NotificationCenter.default.post(name: .floatPin, object: value) }
    }

    private var settingsPopover: some View {
        VStack(alignment: .leading, spacing: 13) {
            Text("显示设置").font(.system(size: 13, weight: .semibold))
            HStack {
                Text("窗口大小").foregroundStyle(.secondary)
                Spacer()
                Button("小") { resize(340, 420) }
                Button("中") { resize(430, 560) }
                Button("大") { resize(620, 720) }
            }
            HStack {
                Text("文字缩放").frame(width: 62, alignment: .leading)
                Slider(value: $fontSize, in: 9.6...18, step: 1.2)
                Text("\(Int((fontSize / 12 * 100).rounded()))%")
                    .monospacedDigit().frame(width: 38, alignment: .trailing)
            }
            HStack {
                Text("玻璃透明度").frame(width: 62, alignment: .leading)
                Slider(value: Binding(get: { 1 - glassOpacity }, set: { glassOpacity = 1 - $0 }), in: 0.2...0.75, step: 0.05)
                Text("\(Int(((1 - glassOpacity) * 100).rounded()))%")
                    .monospacedDigit().frame(width: 38, alignment: .trailing)
            }
            Picker("节省显示", selection: $savingsDisplayMode) {
                Text("自动额度对比").tag("reference")
                Text("数量试算").tag("scenario")
            }.pickerStyle(.segmented)
                .onChange(of: savingsDisplayMode) { _, value in showScenario = value == "scenario" }
            Picker("用量范围", selection: $usageDisplayScope) {
                Text("整个任务").tag("task")
                Text("当前轮次").tag("turn")
            }.pickerStyle(.segmented)
            if savingsDisplayMode == "reference" {
                Text("按已计量用量自动对比全用 Astra 的参考额度；主任务含子代理，折合显示等价 token。")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            DisclosureGroup("数量试算参数", isExpanded: $showScenario) {
                VStack(alignment: .leading, spacing: 9) {
                    HStack {
                        Text("输入缩减").frame(width: 60, alignment: .leading)
                        Slider(value: $inputReduction, in: 0...50, step: 5)
                        Text("\(Int(inputReduction))%")
                            .monospacedDigit().frame(width: 34, alignment: .trailing)
                    }
                    Stepper(value: $delegationOverhead, in: 0...20000, step: 500) {
                        Text("委派额外 \(delegationOverhead.formatted()) tokens")
                    }
                    Text("手动假设的 token 数量变化；仅在“数量试算”模式显示，不影响自动额度对比。")
                        .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }.padding(.top, 7)
            }
            Text("拖动标题栏移动，拖动窗口边缘缩放；大小与位置自动保留。")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .font(.system(size: 11)).padding(15).frame(width: 290)
    }

    private func resize(_ width: Double, _ height: Double) {
        NotificationCenter.default.post(name: .floatResize, object: NSSize(width: width, height: height))
    }
    private func resizeRelative(_ delta: Double) {
        guard let panel = NSApp.windows.first(where: { $0 is FloatingPanel }) else { return }
        resize(max(320, panel.contentLayoutRect.width + delta),
               max(270, panel.contentLayoutRect.height + delta))
    }
    private func age(_ timestamp: Double) -> String {
        let seconds = max(0, Int(model.updated.timeIntervalSince1970 - timestamp))
        if seconds < 60 { return "\(seconds) 秒前" }
        if seconds < 3600 { return "\(seconds / 60) 分钟前" }
        if seconds < 86400 { return "\(seconds / 3600) 小时前" }
        return "\(seconds / 86400) 天前"
    }

    private func taskCard(_ root: Agent) -> some View {
        let children = model.descendants(root.id)
        let expanded = !collapsed.contains(root.id)
        return VStack(alignment: .leading, spacing: 7) {
            agentHeader(root, children: children, expanded: expanded)
            agentMetrics(root)
            if detail == 1 { compactCost(root.usage) }
            if detail == 2 { expandedDetails(root) }
            if !children.isEmpty {
                if expanded {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(children) { child in
                            VStack(alignment: .leading, spacing: 6) {
                                agentHeader(child, children: [], expanded: true)
                                agentMetrics(child)
                                if detail == 1 { compactCost(child.usage) }
                                if detail == 2 { expandedDetails(child) }
                            }
                            .padding(8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.primary.opacity(0.035),
                                        in: RoundedRectangle(cornerRadius: 9))
                            .padding(.leading, child.parent == root.id ? 0 : 10)
                        }
                    }.padding(.top, 2)
                } else {
                    Text("\(children.count) 个子代理 · \(children.filter { $0.busy }.count) 个运行中")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }
            if detail == 2 {
                HStack {
                    Text(URL(fileURLWithPath: root.cwd).lastPathComponent).lineLimit(1).help(root.cwd)
                    Spacer()
                    Button("在 Codex 打开") {
                        if let url = URL(string: "codex://threads/\(root.id)") { NSWorkspace.shared.open(url) }
                    }.buttonStyle(.link).disabled(DemoMode.enabled).help("打开官方 Codex 中的这个任务")
                }.font(.system(size: 10)).foregroundStyle(.secondary)
                Text(root.id).font(.system(size: 8, design: .monospaced))
                    .foregroundStyle(.tertiary).lineLimit(1)
            }
        }
        .padding(detail == 0 ? 9 : 11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12)
            .strokeBorder(Color.primary.opacity(0.09), lineWidth: 1))
    }

    private func agentHeader(_ agent: Agent, children: [Agent], expanded: Bool) -> some View {
        HStack(spacing: 5) {
            if !children.isEmpty {
                Button {
                    if expanded { collapsed.insert(agent.id) } else { collapsed.remove(agent.id) }
                } label: {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .frame(width: 13, height: 17)
                }
                .buttonStyle(.plain).accessibilityLabel("展开或收起 \(agent.title) 的子代理")
            } else if !agent.parent.isEmpty {
                Image(systemName: "arrow.turn.down.right").foregroundStyle(.tertiary)
                    .font(.system(size: 9))
            }
            Text(agent.title).font(.system(size: fontSize, weight: .semibold))
                .lineLimit(1).help(agent.title)
            Spacer(minLength: 2)
            StatusDot(agent: agent)
        }
    }

    private func agentMetrics(_ agent: Agent) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 8) {
                modelAndBadges(agent)
                Spacer(minLength: 2)
                tokenBlock(agent)
            }
            HStack(alignment: .center, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(agent.modelLabel)
                        .font(.system(size: max(9, fontSize - 1), weight: .medium))
                        .lineLimit(1).fixedSize(horizontal: true, vertical: false)
                    HStack(spacing: 5) { effortBadge(agent); difficultyBadge(agent) }
                }
                Spacer(minLength: 2)
                tokenBlock(agent)
            }
            VStack(alignment: .leading, spacing: 5) {
                modelAndBadges(agent)
                tokenBlock(agent).frame(maxWidth: .infinity, alignment: .trailing)
            }
        }
    }

    private func modelAndBadges(_ agent: Agent) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 5) {
                Text(agent.modelLabel)
                    .font(.system(size: max(9, fontSize - 1), weight: .medium))
                    .lineLimit(1).fixedSize(horizontal: true, vertical: false)
                effortBadge(agent)
                difficultyBadge(agent)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(agent.modelLabel)
                    .font(.system(size: max(9, fontSize - 1), weight: .medium))
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 5) {
                    effortBadge(agent)
                    difficultyBadge(agent)
                }
            }
        }
    }

    private func effortBadge(_ agent: Agent) -> some View {
        Text(agent.effortLabel)
            .font(.system(size: max(9, fontSize - 1), weight: .semibold))
            .foregroundStyle(Color.blue)
            .shadow(color: Color.blue.opacity(0.35), radius: 3)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Color.blue.opacity(0.13), in: Capsule())
            .fixedSize()
            .help("任务设置的推理强度")
    }

    private func difficultyBadge(_ agent: Agent) -> some View {
        let score = agent.difficulty?.score
        let color: Color = score.map { $0 <= 3 ? .green : ($0 < 7 ? .yellow : .red) } ?? .secondary
        let foreground: Color = score.map { $0 <= 3 ? .green : ($0 < 7 ? Color(red: 0.55, green: 0.34, blue: 0) : .red) } ?? .secondary
        let source = agent.difficulty?.source
        let label = source == "title_heuristic" ? "标题粗估"
            : (source == "caller_assessment" ? "任务评估" : "规则估分")
        let confidence = agent.difficulty?.confidence ?? "未知"
        let reasons = agent.difficulty?.reasons?.joined(separator: "；") ?? ""
        let explanation = "未校准评价刻度 · \(label) · 置信度：\(confidence) · \(reasons)"
        return Text(score.map { String(format: "难度 %.1f", $0) } ?? "难度待估")
            .font(.system(size: max(11, fontSize + 1.5), weight: .bold))
            .foregroundStyle(foreground)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(color.opacity(0.18), in: Capsule())
            .fixedSize()
            .help(explanation)
    }

    private func tokenBlock(_ agent: Agent) -> some View {
        let current = agent.usage?.currentTurn
        let isTurn = usageDisplayScope == "turn"
        return VStack(alignment: .trailing, spacing: 1) {
            Text(isTurn ? current.map { compactTokens($0.totalTokens) } ?? "—" : compactTokens(agent.tokens))
                .font(.system(size: max(15, fontSize + 4), weight: .bold, design: .rounded))
                .monospacedDigit().foregroundStyle(Color.orange)
                .help(isTurn ? "本轮已计量 \(current?.totalTokens.formatted() ?? "暂无") tokens，按逐请求记录去重。"
                      : "Codex 报告的该任务累计 \(agent.tokens.formatted()) tokens；主子任务勿相加作为账户账单")
            Text(isTurn ? "本轮 tokens" : "累计 tokens")
                .font(.system(size: max(8, fontSize - 3), weight: .medium))
                .foregroundStyle(.secondary)
            if isTurn {
                Text(current?.costUSD.map { String(format: "本轮参考 $%.4f", $0) } ?? "本轮费用 · 待计量")
                    .font(.system(size: max(9, fontSize - 2))).foregroundStyle(.green)
                if current?.partial == true {
                    Text("部分计量").font(.system(size: max(8, fontSize - 4))).foregroundStyle(.secondary)
                }
            } else if savingsDisplayMode == "scenario" {
                scenarioSavingLabel(agent.usage)
            } else {
                referenceSavingLabel(agent.savings)
            }
        }.fixedSize(horizontal: true, vertical: false)
    }

    @ViewBuilder private func referenceSavingLabel(_ saving: ReferenceSavings?) -> some View {
        if let saving, let tokens = saving.equivalentTokens, let percent = saving.percent {
            Text("\(tokens >= 0 ? "折合省" : "折合多用") \(compactTokens(abs(tokens)))"
                 + String(format: " (%.1f%%)", abs(percent)))
                .font(.system(size: max(9, fontSize - 2), weight: .semibold))
                .foregroundStyle(tokens >= 0 ? Color.green : Color.orange)
                .help(referenceSavingExplanation(saving))
            Text("Astra 等价 tokens" + (saving.includesChildren ? " · 含子任务" : "")
                 + (saving.partial ? " · 部分" : ""))
                .font(.system(size: max(8, fontSize - 4))).foregroundStyle(.secondary)
                .help(referenceSavingExplanation(saving))
        } else {
            Text("额度节省 · 待计量")
                .font(.system(size: max(9, fontSize - 2))).foregroundStyle(.secondary)
                .help("等待可定价的本机用量记录；缺失模型价格不会当作免费。")
        }
    }

    private func referenceSavingExplanation(_ saving: ReferenceSavings) -> String {
        let credits = saving.savedCredits.map { String(format: "%.3f", $0) } ?? "待计价"
        let scope = saving.includesChildren ? "含可见子任务，共 \(saving.taskCount) 个任务" : "当前任务"
        return "\(scope)：相同已计量 token 结构全部使用 Astra，与实际模型按 Standard 参考费率相比，差额 \(credits) credits。"
            + "折合 token＝可计价 \(saving.pricedTokens.formatted()) tokens × 额度差额比例；不是实际少生成的 token，也不是订阅扣费。"
            + "\(saving.missingTaskCount) 个任务尚不可计价。" + (saving.partial ? "当前计量不完整，将随日志读取更新。" : "")
    }

    @ViewBuilder private func scenarioSavingLabel(_ usage: Usage?) -> some View {
        if let input = usage?.inputTokens,
           let total = usage?.totalTokens, total > 0,
           (usage?.requests ?? 1) > 0 {
            let saving = Int(floor(Double(input) * inputReduction / 100)) - delegationOverhead
            let ratio = Double(saving) / Double(total) * 100
            Text("\(saving >= 0 ? "试算省" : "试算多用") \(compactTokens(abs(saving)))"
                 + String(format: " (%.1f%%)", abs(ratio)))
                .font(.system(size: max(9, fontSize - 2), weight: .semibold))
                .foregroundStyle(saving >= 0 ? Color.green : Color.orange)
                .help("情景试算：floor(片段输入 tokens × \(Int(inputReduction))%) − 委派额外 \(delegationOverhead) tokens"
                      + "；片段总量 \(total.formatted()) tokens"
                      + "。不是实际节省。")
        } else {
            Text("数量试算 · 待计量")
                .font(.system(size: max(9, fontSize - 2)))
                .foregroundStyle(.secondary)
                .help("片段输入 tokens 尚未计量；情景试算不代表实际节省。")
        }
    }

    private func compactCost(_ usage: Usage?) -> some View {
        Text(shortCost(usage))
            .font(.system(size: 9)).foregroundStyle(.secondary)
            .help("API Standard 短上下文等价估算，非订阅账单；同量 Astra 差额仅比较价格，不能换算成节省 tokens。")
    }

    private func expandedDetails(_ agent: Agent) -> some View {
        let usage = agent.usage
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Text("最近：\(agent.activityLabel)")
                if !agent.tool.isEmpty { Text(agent.tool).lineLimit(1) }
                Spacer(minLength: 0)
                Text(age(agent.updatedAt))
            }
            if agent.status == "stale" { Text("轮次未结束，暂无近期事件").foregroundStyle(.orange) }
            if let route = agent.routeModel, !route.isEmpty {
                Text("工作块建议：\(route)\(agent.routeEffort.map { " · \($0)" } ?? "")")
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let reason = agent.routeReason, !reason.isEmpty {
                Text("选择依据：\(reason)").fixedSize(horizontal: false, vertical: true)
            }
            if let profile = agent.routeProfile, !profile.isEmpty {
                Text("偏好：\(profile) · 验收：\(acceptanceLabel(agent.routeOutcome))")
            }
            if let current = usage?.currentTurn {
                Text("本轮 \(current.totalTokens.formatted()) tokens · \(current.requests) 请求"
                     + (current.costUSD.map { String(format: " · 参考 $%.4f", $0) } ?? " · 暂不可计价"))
            }
            if usage?.persistent == true {
                Text("用量账本已保存在本机；历史请求保留首次计量费率。")
            }
            Text("来源：\(agent.source) · 累计 \(agent.tokens.formatted()) tokens")
                .fixedSize(horizontal: false, vertical: true)
            Text("片段 I \(usage?.inputTokens.map(String.init) ?? "计量中")（输入）/ C \(usage?.cachedInputTokens.map(String.init) ?? "计量中")（I 子集）/ O \(usage?.outputTokens.map(String.init) ?? "计量中")（输出）/ R \(usage?.reasoningTokens.map(String.init) ?? "计量中")（O 子集）· 请求 \(usage?.requests.map(String.init) ?? "计量中")")
                .fixedSize(horizontal: false, vertical: true)
            Text(usageSliceSummary(usage)).fixedSize(horizontal: false, vertical: true)
            if let saving = agent.savings {
                Text(referenceSavingExplanation(saving)).fixedSize(horizontal: false, vertical: true)
            }
            Text("计量片段：\(usage?.coverage ?? "计量中") · 价格依据：\(usage?.priceBasis ?? "暂无") · 价格日期：\(usage?.priceDate ?? "暂无")")
                .fixedSize(horizontal: false, vertical: true)
            Text(costSummary(usage)).fixedSize(horizontal: false, vertical: true)
                .help("API Standard 短上下文等价估算，非订阅账单；同量 Astra 参考差额仅为费用比较。")
        }
        .font(.system(size: 9)).foregroundStyle(.secondary)
    }

    private func compactTokens(_ value: Int) -> String {
        let number = Double(value)
        if value >= 1_000_000 { return String(format: "%.1fM", number / 1_000_000) }
        if value >= 1_000 { return String(format: "%.1fK", number / 1_000) }
        return value.formatted()
    }
    private func acceptanceLabel(_ value: String?) -> String {
        switch value {
        case "accepted": return "通过"
        case "rejected": return "未通过"
        default: return "尚未验收"
        }
    }
    private func shortCost(_ usage: Usage?) -> String {
        guard let cost = usage?.costUSD else { return "片段费用 · 待计价" }
        return String(format: "片段参考 $%.4f", cost)
    }
    private func costSummary(_ usage: Usage?) -> String {
        guard let usage, let usd = usage.costUSD else { return "费用：计量中 / 暂无可计价记录" }
        let credits = usage.credits.map { String(format: "%.2f credits", $0) } ?? "计量中"
        let saving = usage.savingUSD.map { String(format: "；同量全 Astra 参考差额 $%.4f", $0) } ?? ""
        return String(format: "片段参考 $%.4f / %@%@", usd, credits, saving)
    }
    private func usageSliceSummary(_ usage: Usage?) -> String {
        guard let usage else { return "片段已计量：计量中 · 可计价覆盖：暂无可计价记录" }
        let measured = usage.totalTokens.map { "\($0) tokens" } ?? "计量中"
        let priced = usage.pricedTokens.map { "\($0) tokens" } ?? "计量中"
        return "片段已计量 \(measured) · 可计价覆盖 \(priced)"
    }
}

extension Notification.Name {
    static let floatPin = Notification.Name("floatPin")
    static let floatResize = Notification.Name("floatResize")
}
final class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let model = MonitorModel()
    private var panel: FloatingPanel!
    private var statusItem: NSStatusItem!
    func applicationDidFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.register(defaults: ["pinned": true, "glassOpacity": 0.45, "fontSize": 12.0, "detail": 1, "filter": "recent"])
        NSApp.setActivationPolicy(.accessory)
        let panel = FloatingPanel(contentRect: NSRect(x: 0, y: 0, width: 430, height: 560),
            styleMask: [.titled, .closable, .resizable, .nonactivatingPanel], backing: .buffered, defer: false)
        self.panel = panel
        panel.title = DemoMode.enabled ? "Token Inspector · Synthetic Demo" : "Codex Float"
        panel.titlebarAppearsTransparent = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        if DemoMode.enabled {
            panel.appearance = NSAppearance(named: .darkAqua)
            panel.backgroundColor = NSColor(calibratedRed: 0.025, green: 0.06, blue: 0.10, alpha: 0.86)
            panel.setContentSize(NSSize(width: 510, height: 690))
        }
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.level = UserDefaults.standard.bool(forKey: "pinned") ? .floating : .normal
        panel.alphaValue = 1
        panel.minSize = NSSize(width: 320, height: 270)
        let hosting = NSHostingView(rootView: FloatView(model: model).frame(maxWidth: .infinity, maxHeight: .infinity))
        // NSPanel owns its size. Avoid intrinsic-size feedback while switching density.
        hosting.sizingOptions = []
        panel.contentView = hosting
        panel.delegate = self
        if !panel.setFrameUsingName("CodexFloatPanel") {
            if let area = NSScreen.main?.visibleFrame { panel.setFrameOrigin(NSPoint(x: area.maxX - 455, y: area.maxY - 610)) }
        }
        panel.setFrameAutosaveName("CodexFloatPanel")
        confineToScreen()
        let menu = NSMenu()
        menu.addItem(withTitle: "显示 / 隐藏悬浮窗", action: #selector(toggle), keyEquivalent: "")
        menu.addItem(withTitle: "重新连接", action: #selector(reconnect), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 Codex Float", action: #selector(quit), keyEquivalent: "q")
        for item in menu.items { item.target = self }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted", accessibilityDescription: "Codex Float")
        statusItem.menu = menu
        NotificationCenter.default.addObserver(self, selector: #selector(pin(_:)), name: .floatPin, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(resize(_:)), name: .floatResize, object: nil)
        panel.orderFrontRegardless(); model.start()
    }
    @objc private func toggle() { if panel.isVisible { panel.orderOut(nil) } else { panel.orderFrontRegardless() } }
    @objc private func reconnect() { model.stop(); DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.model.start() } }
    @objc private func quit() { NSApp.terminate(nil) }
    @objc private func pin(_ n: Notification) { panel.level = (n.object as? Bool ?? true) ? .floating : .normal }
    @objc private func resize(_ n: Notification) {
        guard let size = n.object as? NSSize else { return }
        let top = panel.frame.maxY
        panel.setContentSize(size)
        panel.setFrameOrigin(NSPoint(x: panel.frame.minX, y: top - panel.frame.height))
        confineToScreen()
    }
    private func confineToScreen() {
        guard let area = (panel.screen ?? NSScreen.main)?.visibleFrame else { return }
        var frame = panel.frame
        frame.size.width = min(frame.width, area.width)
        frame.size.height = min(frame.height, area.height)
        frame.origin.x = max(area.minX, min(frame.minX, area.maxX - frame.width))
        frame.origin.y = max(area.minY, min(frame.minY, area.maxY - frame.height))
        panel.setFrame(frame, display: true)
    }
    func applicationWillTerminate(_ notification: Notification) { model.stop() }
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
}

@main struct CodexFloatApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene { Settings { EmptyView() } }
}
