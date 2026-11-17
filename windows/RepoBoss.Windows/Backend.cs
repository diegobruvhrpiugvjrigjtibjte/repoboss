using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace RepoBoss.Windows;

/// <summary>Persisted app settings (%APPDATA%\RepoBoss\settings.json).</summary>
public sealed class AppSettings
{
    public string? Command { get; set; }
    public string? Home { get; set; }

    static string FilePath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "RepoBoss", "settings.json");

    public static AppSettings Load()
    {
        try
        {
            if (File.Exists(FilePath))
                return JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(FilePath)) ?? new AppSettings();
        }
        catch (Exception)
        {
            // A broken settings file falls back to defaults.
        }
        return new AppSettings();
    }

    public void Save()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        File.WriteAllText(FilePath, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
    }
}

/// <summary>Runs the RepoBoss CLI. Arguments are passed as a list: never through a shell.</summary>
public sealed class Backend
{
    public string Command { get; set; }
    public string Home { get; set; }

    public Backend(string command, string home)
    {
        Command = command;
        Home = home;
    }

    static string SelfPath => Path.GetFullPath(Environment.ProcessPath ?? "");
    static string SelfDir => Path.GetDirectoryName(SelfPath) ?? "";

    static bool IsSelf(string path)
    {
        try
        {
            var full = Path.GetFullPath(path);
            return string.Equals(full, SelfPath, StringComparison.OrdinalIgnoreCase)
                   || string.Equals(Path.GetDirectoryName(full), SelfDir, StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception)
        {
            return true; // unparseable path: never execute it
        }
    }

    /// <summary>
    /// The RepoBoss CLI: repoboss.exe on PATH or in the usual Python script folders (never this app's own
    /// folder), otherwise the py launcher / python with "-m repoboss". Null when nothing usable exists.
    /// </summary>
    public static string? DetectCommand()
    {
        var dirs = (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator).ToList();
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var roaming = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        var profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        foreach (var root in new[] { Path.Combine(local, "Programs", "Python"), Path.Combine(roaming, "Python") })
        {
            if (Directory.Exists(root))
                dirs.AddRange(Directory.GetDirectories(root).Select(d => Path.Combine(d, "Scripts")));
        }
        dirs.Add(Path.Combine(profile, ".local", "bin")); // pipx
        foreach (var dir in dirs)
        {
            try
            {
                if (string.IsNullOrWhiteSpace(dir)) continue;
                var candidate = Path.Combine(dir.Trim(), "repoboss.exe");
                if (File.Exists(candidate) && !IsSelf(candidate)) return candidate;
            }
            catch (ArgumentException)
            {
                // Malformed PATH entry.
            }
        }
        foreach (var launcher in new[] { "py.exe", "python.exe" })
        {
            foreach (var dir in dirs)
            {
                try
                {
                    if (string.IsNullOrWhiteSpace(dir)) continue;
                    var candidate = Path.Combine(dir.Trim(), launcher);
                    if (File.Exists(candidate) && !IsSelf(candidate) && !candidate.Contains("WindowsApps")) return candidate;
                }
                catch (ArgumentException)
                {
                    // Malformed PATH entry.
                }
            }
        }
        return null;
    }

    /// <summary>Full path of the command, or an error message explaining why it cannot be used.</summary>
    (string? exe, string? error) Resolve()
    {
        if (string.IsNullOrWhiteSpace(Command))
            return (null, "RepoBoss command not found. Install it with: pip install git+https://github.com/diegobruvhrpiugvjrigjtibjte/repoboss  — then set it in Settings.");
        string exe = Command.Trim().Trim('"');
        if (!Path.IsPathRooted(exe))
        {
            var found = (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator)
                .Where(d => !string.IsNullOrWhiteSpace(d))
                .Select(d => { try { return Path.Combine(d.Trim(), exe.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? exe : exe + ".exe"); } catch (ArgumentException) { return ""; } })
                .FirstOrDefault(p => p.Length > 0 && File.Exists(p) && !IsSelf(p));
            if (found == null)
                return (null, $"\"{Command}\" was not found on PATH. Set the full path of repoboss.exe (or python.exe) in Settings.");
            exe = found;
        }
        if (!File.Exists(exe)) return (null, $"\"{exe}\" does not exist. Fix the RepoBoss command in Settings.");
        if (IsSelf(exe)) return (null, "The RepoBoss command points to this app itself. Set the path of the RepoBoss CLI (repoboss.exe) in Settings.");
        return (exe, null);
    }

    ProcessStartInfo StartInfo(IEnumerable<string> args)
    {
        var (exe, error) = Resolve();
        if (exe == null) throw new InvalidOperationException(error);
        var psi = new ProcessStartInfo(exe)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["NO_COLOR"] = "1";
        psi.Environment["TERM"] = "dumb";
        psi.Environment["COLUMNS"] = "160";
        var name = Path.GetFileName(exe).ToLowerInvariant();
        if (name is "py.exe" or "python.exe" or "pythonw.exe")
        {
            psi.ArgumentList.Add("-m");
            psi.ArgumentList.Add("repoboss");
        }
        psi.ArgumentList.Add("--home");
        psi.ArgumentList.Add(Home);
        foreach (var a in args) psi.ArgumentList.Add(a);
        return psi;
    }

    public async Task<JsonNode?> JsonAsync(params string[] args)
    {
        using var proc = Process.Start(StartInfo(new[] { "json" }.Concat(args)))
                         ?? throw new InvalidOperationException("cannot start " + Command);
        // Start reading both streams before waiting, otherwise a large output would block the child.
        var stdout = proc.StandardOutput.ReadToEndAsync();
        var stderr = proc.StandardError.ReadToEndAsync();
        var waited = proc.WaitForExitAsync();
        if (await Task.WhenAny(waited, Task.Delay(TimeSpan.FromSeconds(120))) != waited)
        {
            try { proc.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
            throw new InvalidOperationException("the RepoBoss command did not answer within 120 s");
        }
        if (proc.ExitCode != 0)
            throw new InvalidOperationException((await stderr).Trim() is { Length: > 0 } err ? err : "command failed");
        return JsonNode.Parse(await stdout);
    }

    /// <summary>Installs or upgrades the RepoBoss CLI with pip for the given Python (py.exe or python.exe).</summary>
    public static async Task<int> InstallCliAsync(string python, Action<string> onLine)
    {
        var psi = new ProcessStartInfo(python)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        foreach (var a in new[] { "-m", "pip", "install", "--upgrade", "git+https://github.com/diegobruvhrpiugvjrigjtibjte/repoboss" })
            psi.ArgumentList.Add(a);
        using var proc = new Process { StartInfo = psi };
        proc.OutputDataReceived += (_, e) => { if (e.Data != null) onLine(e.Data); };
        proc.ErrorDataReceived += (_, e) => { if (e.Data != null) onLine(e.Data); };
        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        await proc.WaitForExitAsync();
        return proc.ExitCode;
    }

    /// <summary>py.exe or python.exe for installing the CLI, never this app.</summary>
    public static string? FindPython()
    {
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            foreach (var name in new[] { "py.exe", "python.exe" })
            {
                try
                {
                    if (string.IsNullOrWhiteSpace(dir)) continue;
                    var candidate = Path.Combine(dir.Trim(), name);
                    if (File.Exists(candidate) && !IsSelf(candidate) && !candidate.Contains("WindowsApps")) return candidate;
                }
                catch (ArgumentException)
                {
                    // Malformed PATH entry.
                }
            }
        }
        return null;
    }

    /// <summary>Streams merged output line by line; returns the exit code.</summary>
    public async Task<int> StreamAsync(IEnumerable<string> args, Action<string> onLine, Action<Process> onStart)
    {
        using var proc = new Process { StartInfo = StartInfo(args), EnableRaisingEvents = true };
        proc.OutputDataReceived += (_, e) => { if (e.Data != null) onLine(e.Data); };
        proc.ErrorDataReceived += (_, e) => { if (e.Data != null) onLine(e.Data); };
        proc.Start();
        onStart(proc);
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        await proc.WaitForExitAsync();
        return proc.ExitCode;
    }
}

/// <summary>Helpers to read JSON values for display ("n/a" for missing values, never zero).</summary>
public static class J
{
    static readonly HashSet<string> YesNo = new() { "matches_reference", "agent_is_demo", "runner_isolated", "network", "trusted" };

    public static string S(JsonNode? node, string key)
    {
        var v = node?[key];
        if (v is null) return "n/a";
        if (YesNo.Contains(key) && v.GetValueKind() == JsonValueKind.Number)
            return v.GetValue<double>() != 0 ? "yes" : "no";
        var inv = System.Globalization.CultureInfo.InvariantCulture;
        return v.GetValueKind() switch
        {
            JsonValueKind.String => v.GetValue<string>(),
            JsonValueKind.Number => v.GetValue<double>() is var d && d == Math.Floor(d) && Math.Abs(d) < 1e12
                ? ((long)d).ToString(inv)
                : v.GetValue<double>().ToString("0.00", inv),
            JsonValueKind.True => "yes",
            JsonValueKind.False => "no",
            _ => v.ToJsonString(),
        };
    }

    public static bool B(JsonNode? node, string key)
    {
        var v = node?[key];
        if (v is null) return false;
        return v.GetValueKind() switch
        {
            JsonValueKind.True => true,
            JsonValueKind.Number => v.GetValue<double>() != 0,
            _ => false,
        };
    }
}
