using System.Diagnostics;
using System.IO;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Microsoft.Win32;

namespace RepoBoss.Windows;

public sealed record StatRow(string Agent, string Version, string Kind, string Attempts, string Passed, string Rate,
    string Median, string Outcomes);

public sealed record ChallengeRow(string Id, string Name, string Language, string Status, string Trusted, string Runner,
    string Created, JsonNode Node);

public sealed record RunRow(string Id, string Agent, string Challenge, string Outcome, Brush OutcomeBrush, string Tests,
    string Seconds);

public partial class MainWindow : Window
{
    readonly AppSettings _settings = AppSettings.Load();
    readonly Backend _backend;
    readonly string? _screenshot;
    Process? _running;
    string _page = "Overview";

    static readonly Brush OkBrush = (Brush)Application.Current.Resources["Ok"];
    static readonly Brush BadBrush = (Brush)Application.Current.Resources["Bad"];
    static readonly Brush WarnBrush = (Brush)Application.Current.Resources["Warn"];
    static readonly Brush MutedBrush = (Brush)Application.Current.Resources["Muted"];
    static readonly Brush DemoBrush = (Brush)Application.Current.Resources["Demo"];
    static readonly Brush AccentBrush = (Brush)Application.Current.Resources["Accent"];

    public MainWindow(string? home, string? page, string? screenshot, string? commandOverride = null)
    {
        InitializeComponent();
        var command = commandOverride ?? _settings.Command ?? Backend.DetectCommand();
        var ws = home ?? _settings.Home ?? Path.Combine(Environment.CurrentDirectory, ".repoboss");
        _backend = new Backend(command, ws);
        _screenshot = screenshot;
        VersionText.Text = $"v{GetType().Assembly.GetName().Version?.ToString(3)} · Windows";
        Loaded += async (_, _) =>
        {
            var target = NavPanel.Children.OfType<RadioButton>()
                .FirstOrDefault(r => (string)r.Tag == (page ?? "Overview")) ?? NavPanel.Children.OfType<RadioButton>().First();
            target.IsChecked = true;
            if (_screenshot != null)
            {
                await Task.Delay(2500);
                SaveScreenshot(_screenshot);
                Close();
            }
        };
    }

    // ------------------------------------------------------------------------------------------ navigation
    async void Nav_Checked(object sender, RoutedEventArgs e)
    {
        _page = (string)((RadioButton)sender).Tag;
        foreach (var p in new UIElement[] { PageOverview, PageChallenges, PageNew, PageRun, PageRuns, PageCompare, PageSettings })
            p.Visibility = Visibility.Collapsed;
        var (panel, title) = _page switch
        {
            "Challenges" => ((UIElement)PageChallenges, "Challenges"),
            "New" => (PageNew, "New challenge"),
            "Run" => (PageRun, "Run an agent"),
            "Runs" => (PageRuns, "Runs"),
            "Compare" => (PageCompare, "Compare agents"),
            "Settings" => (PageSettings, "Settings"),
            _ => (PageOverview, "Overview"),
        };
        panel.Visibility = Visibility.Visible;
        PageTitle.Text = title;
        WorkspaceText.Text = _backend.Home;
        await LoadPage();
    }

    async Task LoadPage()
    {
        try
        {
            switch (_page)
            {
                case "Overview": await LoadOverview(); break;
                case "Challenges": await LoadChallenges(); break;
                case "Run": await LoadRun(); break;
                case "Runs": await LoadRuns(); break;
                case "Compare": await LoadCompare(); break;
                case "Settings": await LoadSettings(); break;
            }
        }
        catch (Exception ex)
        {
            Log($"Could not read the workspace: {ex.Message}", BadBrush);
            Log("Check Settings: the RepoBoss command and the workspace folder (create it with \"Create workspace\").");
        }
    }

    static IEnumerable<StatRow> ToStats(JsonNode? node) =>
        (node?.AsArray() ?? new JsonArray()).Select(s => new StatRow(
            J.S(s, "agent"), J.S(s, "version"), J.B(s, "is_demo") ? "DEMO" : "real", J.S(s, "attempts"),
            J.S(s, "passed"),
            (s?["success_rate"]?.GetValue<double>() ?? 0).ToString("P0", System.Globalization.CultureInfo.InvariantCulture),
            J.S(s, "median_duration_s"),
            string.Join(", ", (s?["outcomes"]?.AsObject() ?? new JsonObject()).Select(kv => $"{kv.Key}: {kv.Value}"))));

    async Task LoadOverview()
    {
        var challenges = (await _backend.JsonAsync("challenges"))!.AsArray();
        var runs = (await _backend.JsonAsync("runs"))!.AsArray();
        var real = runs.Where(r => !J.B(r, "agent_is_demo")).ToList();
        StatChallenges.Text = challenges.Count.ToString();
        StatLanguages.Text = challenges.Select(c => J.S(c, "language")).Where(l => l != "n/a").Distinct().Count().ToString();
        StatRuns.Text = real.Count.ToString();
        StatPassed.Text = real.Count(r => J.S(r, "outcome") == "passed").ToString();
        StatsGrid.ItemsSource = ToStats(await _backend.JsonAsync("stats")).ToList();
    }

    async Task LoadChallenges()
    {
        var rows = (await _backend.JsonAsync("challenges"))!.AsArray().Select(c => new ChallengeRow(
            J.S(c, "id"), J.S(c, "name"), J.S(c, "language"), J.S(c, "status"), J.B(c, "trusted") ? "yes" : "NO",
            J.S(c, "runner"), J.S(c, "created_at"), c!)).ToList();
        ChallengesGrid.ItemsSource = rows;
        if (rows.Count > 0) ChallengesGrid.SelectedIndex = 0;
    }

    void ChallengesGrid_SelectionChanged(object sender, SelectionChangedEventArgs e) => ShowChallenge(false);

    void ShowChallenge(bool reveal, JsonNode? bug = null)
    {
        if (ChallengesGrid.SelectedItem is not ChallengeRow row) { ChallengeDetail.Text = ""; return; }
        var c = row.Node;
        var cfg = c["config"];
        var lines = new List<string>
        {
            $"{J.S(c, "name")}  ({J.S(c, "id")})",
            $"status: {J.S(c, "status")} - {J.S(c, "status_detail")}",
            $"snapshot: {J.S(c, "snapshot_hash")[..Math.Min(16, J.S(c, "snapshot_hash").Length)]}  seed: {J.S(c, "seed")}  runner: {J.S(c, "runner")}",
            $"test: {cfg?["test"]?.ToJsonString()}  framework: {J.S(cfg, "test_framework")}",
        };
        var v = c["verification"];
        foreach (var label in new[] { "original", "broken", "restored" })
        {
            var rep = v?[label];
            if (rep != null)
                lines.Add($"  {label,-9} {J.S(rep, "status"),-12} passed={J.S(rep, "passed")} failed={J.S(rep, "failed")}");
        }
        if (reveal && bug != null)
            lines.Add($"\nInjected bug: {J.S(bug, "description")} (line {J.S(bug, "line")} of {J.S(c, "bug_path")})");
        ChallengeDetail.Text = string.Join(Environment.NewLine, lines);
    }

    ChallengeRow? SelectedChallenge()
    {
        if (ChallengesGrid.SelectedItem is ChallengeRow row) return row;
        MessageBox.Show(this, "Select a challenge first.", "RepoBoss");
        return null;
    }

    async void ChallengeReveal_Click(object sender, RoutedEventArgs e)
    {
        if (SelectedChallenge() is not { } row) return;
        var full = await _backend.JsonAsync("challenge", row.Id, "--reveal");
        ShowChallenge(true, full?["bug"]);
    }

    void ChallengeRun_Click(object sender, RoutedEventArgs e)
    {
        if (SelectedChallenge() is not { } row) return;
        _preselect = row.Id;
        NavPanel.Children.OfType<RadioButton>().First(r => (string)r.Tag == "Run").IsChecked = true;
    }

    void ChallengeVerify_Click(object sender, RoutedEventArgs e)
    {
        if (SelectedChallenge() is not { } row) return;
        var args = new List<string> { "challenge", "verify", row.Id };
        if (row.Trusted == "NO")
        {
            if (MessageBox.Show(this, "This challenge was imported. Verifying executes its install and test commands. Continue?",
                    "Imported challenge", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
            args.Add("--trust");
        }
        _ = RunCli(args, LoadChallenges);
    }

    void ChallengeExport_Click(object sender, RoutedEventArgs e)
    {
        if (SelectedChallenge() is not { } row) return;
        var dialog = new SaveFileDialog { FileName = $"{row.Id}.zip", Filter = "Challenge package|*.zip" };
        if (dialog.ShowDialog(this) == true) _ = RunCli(new[] { "export", row.Id, "--out", dialog.FileName });
    }

    void ChallengeImport_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog { Filter = "Challenge package|*.zip" };
        if (dialog.ShowDialog(this) == true) _ = RunCli(new[] { "import", dialog.FileName }, LoadChallenges);
    }

    // ------------------------------------------------------------------------------------------ new challenge
    void Browse_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog { Title = "Repository to turn into challenges" };
        if (dialog.ShowDialog(this) == true) RepoBox.Text = dialog.FolderName;
    }

    void Detect_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(RepoBox.Text)) { MessageBox.Show(this, "Choose a repository folder.", "RepoBoss"); return; }
        _ = RunCli(new[] { "detect", "--repo", RepoBox.Text.Trim() });
    }

    void Create_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(RepoBox.Text)) { MessageBox.Show(this, "Choose a repository folder.", "RepoBoss"); return; }
        var args = new List<string> { "challenge", "create", "--repo", RepoBox.Text.Trim(), "--seed", SeedBox.Text.Trim(),
            "--per-transform", PerBox.Text.Trim(), "--runner", ((ComboBoxItem)NewRunner.SelectedItem).Content.ToString()! };
        foreach (var (box, name) in new[] { (TConfig, "config_value"), (TPath, "path_reference"), (TLogic, "logic_condition") })
            if (box.IsChecked == true) { args.Add("--transform"); args.Add(name); }
        _ = RunCli(args);
    }

    // ------------------------------------------------------------------------------------------ run agent
    string? _preselect;
    JsonArray _agents = new();

    async Task LoadRun()
    {
        var challenges = (await _backend.JsonAsync("challenges"))!.AsArray()
            .Where(c => J.S(c, "status") == "accepted" && J.B(c, "trusted"))
            .Select(c => $"{J.S(c, "id")}  {J.S(c, "name")}").ToList();
        RunChallenge.ItemsSource = challenges;
        RunChallenge.SelectedIndex = Math.Max(0, challenges.FindIndex(c => _preselect != null && c.StartsWith(_preselect)));
        _agents = (await _backend.JsonAsync("agents"))!.AsArray();
        var names = _agents.Select(a => J.S(a, "name")).ToList();
        RunAgent.ItemsSource = names;
        RunAgent.SelectedIndex = Math.Max(0, _agents.ToList().FindIndex(a => !J.B(a, "is_demo")));
        var status = await _backend.JsonAsync("status");
        AgentsPath.Text = $"Agents are configured in {J.S(status, "agents_file")} (editable in Settings).";
    }

    void RunAgent_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        var a = _agents.FirstOrDefault(x => J.S(x, "name") == RunAgent.SelectedItem as string);
        if (a == null) { AgentInfo.Text = ""; return; }
        var demo = J.B(a, "is_demo");
        AgentInfo.Foreground = demo ? DemoBrush : MutedBrush;
        AgentInfo.Text = (demo ? "DEMO adapter: verifies the pipeline, does not measure a model. " : "")
                         + J.S(a, "description") + $"  ·  kind: {J.S(a, "kind")}";
    }

    void RunAgent_Click(object sender, RoutedEventArgs e)
    {
        if (RunChallenge.SelectedItem is not string ch) { MessageBox.Show(this, "No accepted challenge yet.", "RepoBoss"); return; }
        var args = new List<string> { "run", ch.Split(' ')[0], "--agent", (string)RunAgent.SelectedItem, "--attempts",
            AttemptsBox.Text.Trim(), "--runner", ((ComboBoxItem)RunRunner.SelectedItem).Content.ToString()! };
        if (!string.IsNullOrWhiteSpace(TimeoutBox.Text)) { args.Add("--timeout"); args.Add(TimeoutBox.Text.Trim()); }
        _ = RunCli(args);
    }

    // ------------------------------------------------------------------------------------------ runs
    async Task LoadRuns()
    {
        var rows = (await _backend.JsonAsync("runs"))!.AsArray().Select(r =>
        {
            var outcome = J.S(r, "outcome");
            var brush = outcome switch { "passed" => OkBrush, "rejected" => WarnBrush, "running" => MutedBrush, _ => BadBrush };
            var agent = J.S(r, "agent_name") + (J.B(r, "agent_is_demo") ? " (DEMO)" : "");
            return new RunRow(J.S(r, "id"), agent, J.S(r, "challenge_id"), outcome, brush,
                $"{J.S(r, "tests_passed")}/{J.S(r, "tests_failed")}/{J.S(r, "tests_errors")}", J.S(r, "total_duration_s"));
        }).ToList();
        RunsGrid.ItemsSource = rows;
        if (rows.Count > 0) RunsGrid.SelectedIndex = 0;
    }

    async void RunsGrid_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (RunsGrid.SelectedItem is not RunRow row) return;
        var r = await _backend.JsonAsync("run", row.Id);
        var doc = new FlowDocument { PagePadding = new Thickness(0), FontFamily = new FontFamily("Cascadia Mono, Consolas"), FontSize = 12 };
        void Line(string text, Brush? brush = null, Brush? bg = null, bool bold = false)
        {
            var p = new Paragraph(new Run(text)) { Margin = new Thickness(0), Foreground = brush ?? new SolidColorBrush(Color.FromRgb(0xCF, 0xD6, 0xDE)) };
            if (bg != null) p.Background = bg;
            if (bold) p.FontWeight = FontWeights.Bold;
            doc.Blocks.Add(p);
        }
        Line($"{J.S(r, "id")}  {J.S(r, "outcome")}", J.S(r, "outcome") == "passed" ? OkBrush : BadBrush, bold: true);
        if (J.B(r, "agent_is_demo")) Line("DEMO adapter: verifies the pipeline, not a model.", DemoBrush);
        if (!J.B(r, "runner_isolated")) Line("Local runner: not isolated.", WarnBrush);
        foreach (var (key, label) in new[] {
            ("agent_name", "Agent"), ("agent_version", "Version"), ("runner", "Runner"), ("attempt_status", "Attempt"),
            ("eval_status", "Evaluation"), ("tests_passed", "Tests passed"), ("tests_failed", "Tests failed"),
            ("files_changed", "Files changed"), ("matches_reference", "Matches reference"),
            ("attempt_duration_s", "Attempt seconds"), ("total_duration_s", "Total seconds"),
            ("input_tokens", "Input tokens"), ("output_tokens", "Output tokens"), ("cost_usd", "Cost (USD)"),
            ("usage_source", "Usage source") })
            Line($"{label,-20} {J.S(r, key)}");
        foreach (var v in r?["violations"]?.AsArray() ?? new JsonArray()) Line($"violation: {v}", BadBrush);
        if (r?["notes"] is { } notes) Line(notes.ToString(), MutedBrush);
        Line("");
        Line("Final patch", AccentBrush, bold: true);
        var patch = r?["patch"]?.ToString();
        foreach (var line in (string.IsNullOrEmpty(patch) ? "(empty patch)" : patch).Split('\n'))
        {
            if (line.StartsWith("+++") || line.StartsWith("---")) Line(line, MutedBrush);
            else if (line.StartsWith("@@")) Line(line, AccentBrush);
            else if (line.StartsWith('+')) Line(line, new SolidColorBrush(Color.FromRgb(0xB9, 0xF3, 0xCF)), new SolidColorBrush(Color.FromRgb(0x14, 0x30, 0x1F)));
            else if (line.StartsWith('-')) Line(line, new SolidColorBrush(Color.FromRgb(0xFF, 0xC9, 0xC4)), new SolidColorBrush(Color.FromRgb(0x3A, 0x1A, 0x18)));
            else Line(line);
        }
        if (r?["agent_log"] is { } log)
        {
            Line("");
            Line("Agent log", AccentBrush, bold: true);
            foreach (var line in log.ToString().Split('\n').TakeLast(400)) Line(line, MutedBrush);
        }
        RunDetail.Document = doc;
    }

    // ------------------------------------------------------------------------------------------ compare
    async Task LoadCompare()
    {
        var items = new List<string> { "All challenges" };
        items.AddRange((await _backend.JsonAsync("challenges"))!.AsArray().Select(c => $"{J.S(c, "id")}  {J.S(c, "name")}"));
        CompareChallenge.ItemsSource = items;
        CompareChallenge.SelectedIndex = 0;
        await LoadCompareData();
    }

    async void CompareChallenge_SelectionChanged(object sender, SelectionChangedEventArgs e) => await LoadCompareData();

    async Task LoadCompareData()
    {
        if (CompareChallenge.SelectedItem is not string pick) return;
        var stats = pick == "All challenges"
            ? await _backend.JsonAsync("stats")
            : await _backend.JsonAsync("stats", "--challenge", pick.Split(' ')[0]);
        CompareGrid.ItemsSource = ToStats(stats).ToList();
        var notes = (stats?.AsArray() ?? new JsonArray()).Where(s => s?["note"] != null)
            .Select(s => $"{J.S(s, "agent")}: {J.S(s, "note")}");
        CompareNote.Text = string.Join(Environment.NewLine, notes.Append(
            "Results apply to these challenges and this environment only; they are not a general ranking."));
    }

    // ------------------------------------------------------------------------------------------ settings
    async Task LoadSettings()
    {
        CommandBox.Text = _backend.Command;
        HomeBox.Text = _backend.Home;
        var status = await _backend.JsonAsync("status");
        if (J.B(status, "initialised"))
        {
            var file = J.S(status, "agents_file");
            AgentsFileText.Text = file;
            AgentsEditor.Text = File.Exists(file) ? await File.ReadAllTextAsync(file) : "";
        }
        else
        {
            AgentsFileText.Text = "No workspace at this location yet: create it above.";
            AgentsEditor.Text = "";
        }
        var docker = status?["docker"];
        Log(J.B(docker, "available") ? $"Docker: {J.S(docker, "detail")}" : $"Docker not available ({J.S(docker, "detail")}): use the local runner for trusted code only.",
            J.B(docker, "available") ? OkBrush : WarnBrush);
    }

    void BrowseCommand_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog { Filter = "Programs|*.exe" };
        if (dialog.ShowDialog(this) == true) CommandBox.Text = dialog.FileName;
    }

    void BrowseHome_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog { Title = "Workspace folder" };
        if (dialog.ShowDialog(this) == true)
            HomeBox.Text = File.Exists(Path.Combine(dialog.FolderName, "repoboss.db")) ? dialog.FolderName : Path.Combine(dialog.FolderName, ".repoboss");
    }

    async void SaveSettings_Click(object sender, RoutedEventArgs e)
    {
        _backend.Command = CommandBox.Text.Trim();
        _backend.Home = HomeBox.Text.Trim();
        _settings.Command = _backend.Command;
        _settings.Home = _backend.Home;
        _settings.Save();
        WorkspaceText.Text = _backend.Home;
        await LoadPage();
    }

    void InitDocker_Click(object sender, RoutedEventArgs e) => _ = RunCli(new[] { "init", "--runner", "docker" }, LoadSettings);
    void InitLocal_Click(object sender, RoutedEventArgs e) => _ = RunCli(new[] { "init", "--runner", "local" }, LoadSettings);
    void Doctor_Click(object sender, RoutedEventArgs e) => _ = RunCli(new[] { "doctor" });

    async void SaveAgents_Click(object sender, RoutedEventArgs e)
    {
        var status = await _backend.JsonAsync("status");
        if (!J.B(status, "initialised")) return;
        await File.WriteAllTextAsync(J.S(status, "agents_file"), AgentsEditor.Text);
        try
        {
            var agents = await _backend.JsonAsync("agents");
            Log("agents.toml saved. Agents: " + string.Join(", ", agents!.AsArray().Select(a => J.S(a, "name"))), OkBrush);
        }
        catch (Exception ex)
        {
            Log("agents.toml saved, but it has an error: " + ex.Message, BadBrush);
        }
    }

    // ------------------------------------------------------------------------------------------ activity
    void Log(string text, Brush? brush = null)
    {
        LogBox.AppendText(text + Environment.NewLine);
        LogBox.ScrollToEnd();
    }

    async Task RunCli(IEnumerable<string> args, Func<Task>? done = null)
    {
        if (_running != null) { MessageBox.Show(this, "Another action is still running.", "RepoBoss"); return; }
        var list = args.ToList();
        LogBox.Clear();
        Log("$ repoboss " + string.Join(" ", list));
        Busy.IsIndeterminate = true;
        StopButton.IsEnabled = true;
        try
        {
            var code = await _backend.StreamAsync(list, line => Dispatcher.Invoke(() => Log(line)), p => _running = p);
            Log($"[finished, exit code {code}]");
        }
        catch (Exception ex)
        {
            Log($"Could not start \"{_backend.Command}\": {ex.Message}. Set the command in Settings.");
        }
        finally
        {
            _running = null;
            Busy.IsIndeterminate = false;
            StopButton.IsEnabled = false;
        }
        if (done != null)
        {
            try { await done(); } catch (Exception ex) { Log(ex.Message); }
        }
    }

    void Stop_Click(object sender, RoutedEventArgs e)
    {
        try { _running?.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
    }

    // ------------------------------------------------------------------------------------------ screenshots
    void SaveScreenshot(string file)
    {
        var dpi = VisualTreeHelper.GetDpi(this);
        var w = (int)(Root.ActualWidth * dpi.DpiScaleX);
        var h = (int)(Root.ActualHeight * dpi.DpiScaleY);
        var bmp = new RenderTargetBitmap(w, h, 96 * dpi.DpiScaleX, 96 * dpi.DpiScaleY, PixelFormats.Pbgra32);
        var visual = new DrawingVisual();
        using (var ctx = visual.RenderOpen())
        {
            ctx.DrawRectangle((Brush)Application.Current.Resources["Bg"], null, new Rect(0, 0, Root.ActualWidth, Root.ActualHeight));
            ctx.DrawRectangle(new VisualBrush(Root), null, new Rect(0, 0, Root.ActualWidth, Root.ActualHeight));
        }
        bmp.Render(visual);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bmp));
        using var stream = File.Create(file);
        encoder.Save(stream);
    }
}
