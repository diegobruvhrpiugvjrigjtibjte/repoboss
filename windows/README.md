# RepoBoss for Windows (WPF, .NET 8)

A native Windows 10/11 interface for RepoBoss, with these sections:

| Section | What it does |
|---|---|
| Overview | Statistics and per-agent results |
| Challenges | Re-verify, reveal the bug, export or import, run an agent on a challenge |
| New challenge | Folder picker, project detection, bug types, seed, runner |
| Run agent | Pick a challenge and an agent; streams output live |
| Runs | Results, colored patch and agent log |
| Compare | Results side by side |
| Settings | Command, workspace, `agents.toml` editor |

It reads data with `repoboss json …` and runs actions with the normal CLI. Arguments are passed as a
list; there is never a shell in between.

## Install

1. Install Python 3.12+ and RepoBoss: `pip install git+https://github.com/diegobruvhrpiugvjrigjtibjte/repoboss`.
2. Install the [.NET 8 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet/8.0).
3. Download `RepoBoss-Windows-x64.zip` from the releases page, extract it and run `RepoBoss.exe`.
4. In **Settings**, check the RepoBoss command (`repoboss.exe` is found on PATH automatically) and
   choose or create a workspace.

## Build from source

```powershell
dotnet publish windows/RepoBoss.Windows -c Release -r win-x64 --self-contained false `
  -p:PublishSingleFile=true -o dist/windows
```

For documentation images, the app also takes `--home DIR --command PATH --page Runs --screenshot out.png`.
