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

1. Install the [.NET 8 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet/8.0) and
   [Python 3.12+](https://www.python.org/downloads/) (tick "Add to PATH").
2. Download `RepoBoss-Windows-x64.zip` from the releases page, extract it and run `RepoBossApp.exe`.
3. In **Settings**, press **Install RepoBoss CLI (pip)**. This installs the engine. If you already
   installed it with pip, `repoboss.exe` is found automatically.
4. Press **Create workspace**, then go to **New challenge**.

The app never runs itself as the RepoBoss command:
- it ignores its own folder when looking for `repoboss.exe`;
- it exits immediately if started with command-line arguments;
- only one window can be open at a time.

## Build from source

```powershell
dotnet publish windows/RepoBoss.Windows -c Release -r win-x64 --self-contained false `
  -p:PublishSingleFile=true -o dist/windows
```

For documentation images, the app also takes `--home DIR --command PATH --page Runs --screenshot out.png`.
