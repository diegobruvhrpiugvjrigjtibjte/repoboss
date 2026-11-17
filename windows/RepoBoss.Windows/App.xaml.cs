using System.Windows;

namespace RepoBoss.Windows;

public partial class App : Application
{
    System.Threading.Mutex? _mutex;

    /// <summary>Command line: [--home DIR] [--page NAME] [--screenshot FILE.png] (documentation screenshots).</summary>
    static readonly HashSet<string> OwnFlags = new() { "--home", "--page", "--screenshot", "--command" };

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        // Guard: if this app is ever started with CLI-style arguments (e.g. "json", "run"), it was launched by
        // mistake in place of the RepoBoss CLI. Exit at once instead of opening a window.
        for (var i = 0; i < e.Args.Length; i++)
        {
            if (OwnFlags.Contains(e.Args[i])) { i++; continue; }
            Shutdown(2);
            return;
        }
        // Single instance: a second launch just exits.
        _mutex = new System.Threading.Mutex(true, "RepoBoss.Windows.SingleInstance", out var first);
        if (!first && !e.Args.Contains("--screenshot"))
        {
            MessageBox.Show("RepoBoss is already open.", "RepoBoss");
            Shutdown(0);
            return;
        }
        string? home = null, page = null, screenshot = null, command = null;
        for (var i = 0; i < e.Args.Length - 1; i++)
        {
            switch (e.Args[i])
            {
                case "--home": home = e.Args[i + 1]; break;
                case "--page": page = e.Args[i + 1]; break;
                case "--screenshot": screenshot = e.Args[i + 1]; break;
                case "--command": command = e.Args[i + 1]; break;
            }
        }
        var window = new MainWindow(home, page, screenshot, command);
        window.Show();
    }
}
