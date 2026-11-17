import SwiftUI

@main
struct RepoBossApp: App {
    @StateObject private var backend = Backend()

    var body: some Scene {
        WindowGroup("RepoBoss") {
            ContentView()
                .environmentObject(backend)
                .frame(minWidth: 1000, minHeight: 680)
                .preferredColorScheme(.dark)
        }
    }
}

enum Page: String, CaseIterable, Identifiable {
    case overview = "Overview", challenges = "Challenges", newChallenge = "New challenge", run = "Run agent",
         runs = "Runs", compare = "Compare", settings = "Settings"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .overview: return "square.grid.2x2"
        case .challenges: return "flag.checkered"
        case .newChallenge: return "plus.square"
        case .run: return "play.circle"
        case .runs: return "list.bullet.rectangle"
        case .compare: return "chart.bar"
        case .settings: return "gearshape"
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var backend: Backend
    @State private var page: Page? = .overview
    @State private var preselect: String?

    var body: some View {
        NavigationSplitView {
            List(Page.allCases, selection: $page) { item in
                Label(item.rawValue, systemImage: item.icon).tag(item)
            }
            .navigationSplitViewColumnWidth(min: 190, ideal: 210)
            .safeAreaInset(edge: .bottom) {
                Text("Demo adapters verify the pipeline, not a model.")
                    .font(.caption).foregroundStyle(.purple).padding(12)
            }
        } detail: {
            VStack(spacing: 0) {
                Group {
                    switch page ?? .overview {
                    case .overview: OverviewView()
                    case .challenges: ChallengesView(onRun: { id in preselect = id; page = .run })
                    case .newChallenge: NewChallengeView()
                    case .run: RunAgentView(preselect: preselect)
                    case .runs: RunsView()
                    case .compare: CompareView()
                    case .settings: SettingsView()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .padding(20)
                ActivityView()
            }
            .navigationTitle((page ?? .overview).rawValue)
        }
    }
}

struct ActivityView: View {
    @EnvironmentObject var backend: Backend
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Activity").foregroundStyle(.secondary)
                Spacer()
                if backend.busy { ProgressView().controlSize(.small) }
                Button("Stop") { backend.stop() }.disabled(!backend.busy)
            }
            ScrollView {
                Text(backend.log.isEmpty ? " " : backend.log)
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .frame(height: 150)
            .background(Color.black.opacity(0.35))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .padding([.horizontal, .bottom], 20)
    }
}

struct Row: Identifiable {
    let id: String
    let values: [String: Any]
}

struct StatCard: View {
    let value: String
    let label: String
    var color: Color = .blue
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value).font(.system(size: 30, weight: .bold)).foregroundStyle(color)
            Text(label).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

struct OverviewView: View {
    @EnvironmentObject var backend: Backend
    @State private var challenges: [[String: Any]] = []
    @State private var runs: [[String: Any]] = []
    @State private var stats: [Row] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            let real = runs.filter { !Show.flag($0, "agent_is_demo") }
            HStack(spacing: 12) {
                StatCard(value: "\(challenges.count)", label: "Challenges")
                StatCard(value: "\(Set(challenges.map { Show.s($0, "language") }).subtracting(["n/a"]).count)", label: "Languages")
                StatCard(value: "\(real.count)", label: "Runs by real agents")
                StatCard(value: "\(real.filter { Show.s($0, "outcome") == "passed" }.count)", label: "Passed (real agents)",
                         color: .green)
            }
            Text("Per agent, all challenges. Results describe this environment; they are not a general ranking.")
                .foregroundStyle(.secondary)
            StatsTable(rows: stats)
        }
        .task {
            challenges = await backend.array("challenges")
            runs = await backend.array("runs")
            stats = await backend.array("stats").enumerated().map { Row(id: "\($0.offset)", values: $0.element) }
        }
    }
}

struct StatsTable: View {
    let rows: [Row]
    var body: some View {
        Table(rows) {
            TableColumn("Agent") { Text(Show.s($0.values, "agent")) }
            TableColumn("Version") { Text(Show.s($0.values, "version")) }
            TableColumn("Kind") { Text(Show.flag($0.values, "is_demo") ? "DEMO" : "real") }
            TableColumn("Attempts") { Text(Show.s($0.values, "attempts")) }
            TableColumn("Passed") { Text(Show.s($0.values, "passed")) }
            TableColumn("Rate") { row in
                Text(String(format: "%.0f%%", ((row.values["success_rate"] as? Double) ?? 0) * 100))
            }
            TableColumn("Median s") { Text(Show.s($0.values, "median_duration_s")) }
        }
    }
}

struct ChallengesView: View {
    @EnvironmentObject var backend: Backend
    let onRun: (String) -> Void
    @State private var rows: [Row] = []
    @State private var selection: String?
    @State private var detail = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Table(rows, selection: $selection) {
                TableColumn("ID") { Text(Show.s($0.values, "id")) }
                TableColumn("Name") { Text(Show.s($0.values, "name")) }
                TableColumn("Language") { Text(Show.s($0.values, "language")) }
                TableColumn("Status") { row in
                    Text(Show.s(row.values, "status"))
                        .foregroundStyle(Show.s(row.values, "status") == "accepted" ? .green : .orange)
                }
                TableColumn("Trusted") { Text(Show.flag($0.values, "trusted") ? "yes" : "NO") }
                TableColumn("Created") { Text(Show.s($0.values, "created_at")) }
            }
            HStack {
                Button("Run an agent on it") { if let id = selection { onRun(id) } }.buttonStyle(.borderedProminent)
                Button("Re-verify") {
                    guard let id = selection, let row = rows.first(where: { $0.id == id }) else { return }
                    var args = ["challenge", "verify", id]
                    if !Show.flag(row.values, "trusted") { args.append("--trust") }
                    backend.run(args) { Task { await load() } }
                }
                Button("Reveal bug") {
                    guard let id = selection else { return }
                    Task {
                        let full = (try? await backend.json("challenge", id, "--reveal")) as? [String: Any]
                        let bug = full?["bug"] as? [String: Any] ?? [:]
                        detail += "\nInjected bug: \(Show.s(bug, "description")) (line \(Show.s(bug, "line")))"
                    }
                }
            }
            ScrollView {
                Text(detail).font(.system(.body, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .frame(height: 150)
        }
        .task { await load() }
        .onChange(of: selection) { _ in describe() }
    }

    func load() async {
        rows = await backend.array("challenges").map { Row(id: Show.s($0, "id"), values: $0) }
        selection = rows.first?.id
        describe()
    }

    func describe() {
        guard let id = selection, let row = rows.first(where: { $0.id == id }) else { detail = ""; return }
        var lines = ["\(Show.s(row.values, "name"))  (\(id))",
                     "status: \(Show.s(row.values, "status")) - \(Show.s(row.values, "status_detail"))"]
        if let v = row.values["verification"] as? [String: Any] {
            for label in ["original", "broken", "restored"] {
                if let rep = v[label] as? [String: Any] {
                    lines.append("  \(label)  \(Show.s(rep, "status"))  passed=\(Show.s(rep, "passed")) failed=\(Show.s(rep, "failed"))")
                }
            }
        }
        detail = lines.joined(separator: "\n")
    }
}

struct NewChallengeView: View {
    @EnvironmentObject var backend: Backend
    @State private var repo = ""
    @State private var transforms: Set<String> = ["config_value", "path_reference", "logic_condition"]
    @State private var seed = 0
    @State private var per = 1
    @State private var runner = "docker"

    var body: some View {
        Form {
            HStack {
                TextField("Repository folder", text: $repo)
                Button("Browse…") {
                    let panel = NSOpenPanel()
                    panel.canChooseDirectories = true
                    panel.canChooseFiles = false
                    if panel.runModal() == .OK, let url = panel.url { repo = url.path }
                }
                Button("Detect") { backend.run(["detect", "--repo", repo]) }
            }
            HStack {
                ForEach(["config_value", "path_reference", "logic_condition"], id: \.self) { name in
                    Toggle(name, isOn: Binding(
                        get: { transforms.contains(name) },
                        set: { if $0 { transforms.insert(name) } else { transforms.remove(name) } }))
                }
            }
            Stepper("Seed: \(seed)", value: $seed, in: 0...99_999)
            Stepper("Challenges per bug type: \(per)", value: $per, in: 1...10)
            Picker("Runner", selection: $runner) {
                Text("docker (isolated)").tag("docker")
                Text("local (NOT isolated)").tag("local")
            }
            Text("Without a repoboss.toml the project type is detected automatically. Creating a challenge runs the test suite several times.")
                .foregroundStyle(.secondary)
            Button("Create challenges") {
                var args = ["challenge", "create", "--repo", repo, "--seed", "\(seed)", "--per-transform", "\(per)",
                            "--runner", runner]
                for name in transforms.sorted() { args += ["--transform", name] }
                backend.run(args)
            }
            .buttonStyle(.borderedProminent)
            .disabled(repo.isEmpty)
        }
    }
}

struct RunAgentView: View {
    @EnvironmentObject var backend: Backend
    let preselect: String?
    @State private var challenges: [[String: Any]] = []
    @State private var agents: [[String: Any]] = []
    @State private var challenge = ""
    @State private var agent = ""
    @State private var attempts = 1
    @State private var runner = "docker"

    var body: some View {
        Form {
            Picker("Challenge", selection: $challenge) {
                ForEach(challenges.indices, id: \.self) { i in
                    Text("\(Show.s(challenges[i], "id"))  \(Show.s(challenges[i], "name"))").tag(Show.s(challenges[i], "id"))
                }
            }
            Picker("Agent", selection: $agent) {
                ForEach(agents.indices, id: \.self) { i in
                    let a = agents[i]
                    Text(Show.s(a, "name") + (Show.flag(a, "is_demo") ? " (DEMO)" : "")).tag(Show.s(a, "name"))
                }
            }
            if let a = agents.first(where: { Show.s($0, "name") == agent }) {
                Text((Show.flag(a, "is_demo") ? "DEMO adapter: verifies the pipeline, does not measure a model. " : "")
                     + Show.s(a, "description"))
                    .foregroundStyle(Show.flag(a, "is_demo") ? .purple : .secondary)
            }
            Stepper("Attempts: \(attempts)", value: $attempts, in: 1...50)
            Picker("Runner", selection: $runner) {
                Text("docker (isolated)").tag("docker")
                Text("local (NOT isolated)").tag("local")
            }
            Button("Run") {
                backend.run(["run", challenge, "--agent", agent, "--attempts", "\(attempts)", "--runner", runner])
            }
            .buttonStyle(.borderedProminent)
            .disabled(challenge.isEmpty || agent.isEmpty)
        }
        .task {
            challenges = await backend.array("challenges").filter {
                Show.s($0, "status") == "accepted" && Show.flag($0, "trusted")
            }
            agents = await backend.array("agents")
            challenge = preselect ?? challenges.first.map { Show.s($0, "id") } ?? ""
            agent = agents.first(where: { !Show.flag($0, "is_demo") }).map { Show.s($0, "name") }
                ?? agents.first.map { Show.s($0, "name") } ?? ""
        }
    }
}

struct RunsView: View {
    @EnvironmentObject var backend: Backend
    @State private var rows: [Row] = []
    @State private var selection: String?
    @State private var detail: [String: Any] = [:]

    var body: some View {
        HSplitView {
            Table(rows, selection: $selection) {
                TableColumn("Run") { Text(Show.s($0.values, "id")) }
                TableColumn("Agent") { row in
                    Text(Show.s(row.values, "agent_name") + (Show.flag(row.values, "agent_is_demo") ? " (DEMO)" : ""))
                }
                TableColumn("Outcome") { row in
                    let outcome = Show.s(row.values, "outcome")
                    Text(outcome).foregroundStyle(outcome == "passed" ? .green : outcome == "rejected" ? .orange : .red)
                }
                TableColumn("Seconds") { Text(Show.s($0.values, "total_duration_s")) }
            }
            .frame(minWidth: 380)
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(lines(), id: \.offset) { item in
                        Text(item.element.0)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(item.element.1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(8)
                .textSelection(.enabled)
            }
            .background(Color.black.opacity(0.35))
        }
        .task {
            rows = await backend.array("runs").map { Row(id: Show.s($0, "id"), values: $0) }
            selection = rows.first?.id
        }
        .onChange(of: selection) { id in
            guard let id else { return }
            Task { detail = ((try? await backend.json("run", id)) as? [String: Any]) ?? [:] }
        }
    }

    func lines() -> [(offset: Int, element: (String, Color))] {
        guard !detail.isEmpty else { return [] }
        var out: [(String, Color)] = [("\(Show.s(detail, "id"))  \(Show.s(detail, "outcome"))",
                                      Show.s(detail, "outcome") == "passed" ? .green : .red)]
        if Show.flag(detail, "agent_is_demo") { out.append(("DEMO adapter: verifies the pipeline, not a model.", .purple)) }
        for (key, label) in [("agent_version", "Version"), ("eval_status", "Evaluation"), ("tests_passed", "Tests passed"),
                             ("tests_failed", "Tests failed"), ("input_tokens", "Input tokens"),
                             ("output_tokens", "Output tokens"), ("cost_usd", "Cost (USD)"),
                             ("usage_source", "Usage source")] {
            out.append(("\(label.padding(toLength: 16, withPad: " ", startingAt: 0)) \(Show.s(detail, key))", .primary))
        }
        out.append(("", .primary))
        out.append(("Final patch", .blue))
        let patch = (detail["patch"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "(empty patch)"
        for line in patch.split(separator: "\n", omittingEmptySubsequences: false).map(String.init) {
            let color: Color = line.hasPrefix("+++") || line.hasPrefix("---") ? .secondary
                : line.hasPrefix("@@") ? .blue : line.hasPrefix("+") ? .green : line.hasPrefix("-") ? .red : .primary
            out.append((line, color))
        }
        return Array(out.enumerated()).map { (offset: $0.offset, element: $0.element) }
    }
}

struct CompareView: View {
    @EnvironmentObject var backend: Backend
    @State private var challenges: [[String: Any]] = []
    @State private var choice = "all"
    @State private var stats: [Row] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("Challenge", selection: $choice) {
                Text("All challenges").tag("all")
                ForEach(challenges.indices, id: \.self) { i in
                    Text("\(Show.s(challenges[i], "id"))  \(Show.s(challenges[i], "name"))").tag(Show.s(challenges[i], "id"))
                }
            }
            .frame(maxWidth: 620)
            StatsTable(rows: stats)
            Text("Results apply to these challenges and this environment only; they are not a general ranking.")
                .foregroundStyle(.secondary)
        }
        .task {
            challenges = await backend.array("challenges")
            await load()
        }
        .onChange(of: choice) { _ in Task { await load() } }
    }

    func load() async {
        let data = choice == "all" ? await backend.array("stats") : await backend.array("stats", "--challenge", choice)
        stats = data.enumerated().map { Row(id: "\($0.offset)", values: $0.element) }
    }
}

struct SettingsView: View {
    @EnvironmentObject var backend: Backend
    @State private var agentsFile = ""
    @State private var agentsText = ""

    var body: some View {
        Form {
            TextField("RepoBoss command", text: $backend.command)
            TextField("Workspace folder (.repoboss)", text: $backend.home)
            HStack {
                Button("Save") { backend.save(); Task { await load() } }.buttonStyle(.borderedProminent)
                Button("Create workspace (docker)") { backend.run(["init", "--runner", "docker"]) { Task { await load() } } }
                Button("Create workspace (local)") { backend.run(["init", "--runner", "local"]) { Task { await load() } } }
                Button("Doctor") { backend.run(["doctor"]) }
            }
            if !agentsFile.isEmpty {
                Text(agentsFile).foregroundStyle(.secondary)
                TextEditor(text: $agentsText).font(.system(.body, design: .monospaced)).frame(minHeight: 260)
                Button("Save agents.toml") {
                    try? agentsText.write(toFile: agentsFile, atomically: true, encoding: .utf8)
                    backend.run(["json", "agents"])
                }
            }
        }
        .task { await load() }
    }

    func load() async {
        let status = ((try? await backend.json("status")) as? [String: Any]) ?? [:]
        agentsFile = (status["agents_file"] as? String) ?? ""
        agentsText = agentsFile.isEmpty ? "" : ((try? String(contentsOfFile: agentsFile, encoding: .utf8)) ?? "")
    }
}
