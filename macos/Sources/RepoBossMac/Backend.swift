import Foundation

/// Runs the RepoBoss CLI. Arguments are passed as an array: never through a shell.
final class Backend: ObservableObject {
    @Published var command: String
    @Published var home: String
    @Published var log: String = ""
    @Published var busy = false
    private var running: Process?

    init() {
        let defaults = UserDefaults.standard
        command = defaults.string(forKey: "command") ?? Backend.detectCommand()
        home = defaults.string(forKey: "home")
            ?? (FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".repoboss").path)
    }

    func save() {
        UserDefaults.standard.set(command, forKey: "command")
        UserDefaults.standard.set(home, forKey: "home")
    }

    /// repoboss on the usual PATH locations (Homebrew, pipx, user installs).
    static func detectCommand() -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = ["/opt/homebrew/bin/repoboss", "/usr/local/bin/repoboss", "\(home)/.local/bin/repoboss"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "repoboss"
    }

    private func makeProcess(_ args: [String]) -> Process {
        let process = Process()
        if command.hasPrefix("/") {
            process.executableURL = URL(fileURLWithPath: command)
            process.arguments = ["--home", home] + args
        } else {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = [command, "--home", home] + args
        }
        var env = ProcessInfo.processInfo.environment
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        env["COLUMNS"] = "160"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        process.environment = env
        return process
    }

    /// `repoboss json ...` decoded with JSONSerialization.
    func json(_ args: String...) async throws -> Any {
        let process = makeProcess(["json"] + args)
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        try process.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            let message = String(data: errData, encoding: .utf8) ?? "command failed"
            throw NSError(domain: "RepoBoss", code: Int(process.terminationStatus),
                          userInfo: [NSLocalizedDescriptionKey: message.trimmingCharacters(in: .whitespacesAndNewlines)])
        }
        return try JSONSerialization.jsonObject(with: data)
    }

    func array(_ args: String...) async -> [[String: Any]] {
        do {
            let value: Any
            switch args.count {
            case 1: value = try await json(args[0])
            case 2: value = try await json(args[0], args[1])
            default: value = try await json(args[0], args[1], args[2])
            }
            return value as? [[String: Any]] ?? []
        } catch {
            await append("Could not read the workspace: \(error.localizedDescription)\n")
            return []
        }
    }

    @MainActor func append(_ text: String) {
        log += text
    }

    /// Streams output into `log`; calls `done` on the main actor when finished.
    func run(_ args: [String], done: (() -> Void)? = nil) {
        guard running == nil else { return }
        log = "$ repoboss " + args.joined(separator: " ") + "\n"
        busy = true
        let process = makeProcess(args)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self.log += text }
        }
        process.terminationHandler = { proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            Task { @MainActor in
                self.log += "[finished, exit code \(proc.terminationStatus)]\n"
                self.busy = false
                self.running = nil
                done?()
            }
        }
        do {
            try process.run()
            running = process
        } catch {
            log += "Could not start \"\(command)\": \(error.localizedDescription). Set the command in Settings.\n"
            busy = false
        }
    }

    func stop() {
        running?.terminate()
    }
}

/// Display helpers: missing values are "n/a", never zero.
enum Show {
    static func s(_ row: [String: Any], _ key: String) -> String {
        guard let value = row[key], !(value is NSNull) else { return "n/a" }
        if let number = value as? NSNumber {
            if CFGetTypeID(number) == CFBooleanGetTypeID() { return number.boolValue ? "yes" : "no" }
            let d = number.doubleValue
            return d == d.rounded() ? String(Int(d)) : String(format: "%.2f", d)
        }
        return "\(value)"
    }

    static func flag(_ row: [String: Any], _ key: String) -> Bool {
        if let n = row[key] as? NSNumber { return n.boolValue }
        return false
    }
}
