using System.Windows;

namespace RepoBoss.Windows;

public partial class App : Application
{
    /// <summary>Command line: [--home DIR] [--page NAME] [--screenshot FILE.png] (documentation screenshots).</summary>
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
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
