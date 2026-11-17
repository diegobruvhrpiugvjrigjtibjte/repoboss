# RepoBoss for macOS (SwiftUI)

A native macOS interface with the same sections as the Windows app: Overview, Challenges, New
challenge, Run agent, Runs, Compare and Settings. It drives the `repoboss` command and reads data
through `repoboss json …`, so results are identical across interfaces.

> **Status: source only, not built yet.** This app was written on Windows and has not been compiled
> or run on a Mac. Expect small fixes on the first build. Until then, the cross-platform desktop app
> `repoboss gui` (Tkinter) and the terminal interfaces work on macOS today.

## Build

You need macOS 13+ and Xcode 15 (or the Swift 5.9 toolchain). Then:

```sh
pip install repoboss            # or: pip install -e /path/to/repoboss
./build-app.sh                  # creates build/RepoBoss.app (unsigned)
open build/RepoBoss.app
```

In Settings, set the RepoBoss command (for example `/opt/homebrew/bin/repoboss`) and the workspace
folder.
