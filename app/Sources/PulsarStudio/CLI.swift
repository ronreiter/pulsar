import Foundation

/// Runs the Python `pulsar` command-line tool.
enum CLI {
    static var executable: String {
        if let env = ProcessInfo.processInfo.environment["PULSAR_BIN"], !env.isEmpty { return env }
        if let plist = Bundle.main.object(forInfoDictionaryKey: "PulsarCLI") as? String,
           FileManager.default.isExecutableFile(atPath: plist) { return plist }
        return FileManager.default.homeDirectoryForCurrentUser.path + "/GitHub/pulsar/.venv/bin/pulsar"
    }

    struct Result { let status: Int32; let stdout: Data?; let stderr: String }

    /// Run to completion (call off the main thread).
    static func run(_ args: [String], stdin: String) -> Result {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: executable)
        p.arguments = args
        let inPipe = Pipe(), outPipe = Pipe(), errPipe = Pipe()
        p.standardInput = inPipe
        p.standardOutput = outPipe
        p.standardError = errPipe
        do { try p.run() } catch { return Result(status: -1, stdout: nil, stderr: "\(error)") }
        inPipe.fileHandleForWriting.write(Data(stdin.utf8))
        try? inPipe.fileHandleForWriting.close()
        let out = outPipe.fileHandleForReading.readDataToEndOfFile()
        let err = errPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return Result(status: p.terminationStatus, stdout: out, stderr: String(data: err, encoding: .utf8) ?? "")
    }

    /// Start a long-running command, streaming stderr lines.
    @discardableResult
    static func start(_ args: [String], stdin: String, onLine: @escaping @Sendable (String) -> Void,
                      completion: @escaping @Sendable (Int32, String) -> Void) -> Process? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: executable)
        p.arguments = args
        let inPipe = Pipe(), errPipe = Pipe()
        p.standardInput = inPipe
        p.standardOutput = FileHandle.nullDevice
        p.standardError = errPipe
        let buffer = LineBuffer()
        errPipe.fileHandleForReading.readabilityHandler = { h in
            let d = h.availableData
            if d.isEmpty { return }
            for line in buffer.feed(d) { onLine(line) }
        }
        p.terminationHandler = { proc in
            errPipe.fileHandleForReading.readabilityHandler = nil
            let rest = errPipe.fileHandleForReading.readDataToEndOfFile()
            _ = buffer.feed(rest)
            completion(proc.terminationReason == .uncaughtSignal ? 15 : proc.terminationStatus, buffer.all)
        }
        do { try p.run() } catch { completion(-1, "\(error)"); return nil }
        inPipe.fileHandleForWriting.write(Data(stdin.utf8))
        try? inPipe.fileHandleForWriting.close()
        return p
    }
}

final class LineBuffer: @unchecked Sendable {
    private var partial = ""
    private(set) var all = ""
    private let lock = NSLock()

    func feed(_ d: Data) -> [String] {
        lock.lock(); defer { lock.unlock() }
        let s = String(data: d, encoding: .utf8) ?? ""
        all += s
        partial += s
        var lines = partial.components(separatedBy: "\n")
        partial = lines.removeLast()
        return lines
    }
}
