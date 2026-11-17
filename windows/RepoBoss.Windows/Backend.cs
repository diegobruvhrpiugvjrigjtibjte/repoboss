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

    /// <summary>repoboss.exe on PATH, or "python -m repoboss" via the py launcher.</summary>
    public static string DetectCommand()
    {
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            try
            {
                var candidate = Path.Combine(dir.Trim(), "repoboss.exe");
                if (File.Exists(candidate)) return candidate;
            }
            catch (ArgumentException)
            {
                // Malformed PATH entry.
            }
        }
        return "repoboss";
    }

    ProcessStartInfo StartInfo(IEnumerable<string> args)
    {
        var psi = new ProcessStartInfo(Command)
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
        psi.ArgumentList.Add("--home");
        psi.ArgumentList.Add(Home);
        foreach (var a in args) psi.ArgumentList.Add(a);
        return psi;
    }

    public async Task<JsonNode?> JsonAsync(params string[] args)
    {
        using var proc = Process.Start(StartInfo(new[] { "json" }.Concat(args)))
                         ?? throw new InvalidOperationException("cannot start " + Command);
        var stdout = proc.StandardOutput.ReadToEndAsync();
        var stderr = proc.StandardError.ReadToEndAsync();
        await proc.WaitForExitAsync();
        if (proc.ExitCode != 0)
            throw new InvalidOperationException((await stderr).Trim() is { Length: > 0 } err ? err : "command failed");
        return JsonNode.Parse(await stdout);
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
