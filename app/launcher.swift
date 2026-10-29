// The executable of transwhisper.app: runs main.py from the project folder with its .venv.
// Python runs as a child process, so macOS asks for the system audio permission on behalf of
// transwhisper.app (the way it does for a terminal), and the code stays in the project folder.
import Foundation

guard let project = Bundle.main.object(forInfoDictionaryKey: "TranswhisperProject") as? String else {
    FileHandle.standardError.write("TranswhisperProject is missing in Info.plist\n".data(using: .utf8)!)
    exit(1)
}

// There is no console: the output of the last run goes to ~/Library/Logs/transwhisper.log.
let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/transwhisper.log")
FileManager.default.createFile(atPath: logURL.path, contents: nil)
let log = try FileHandle(forWritingTo: logURL)

let child = Process()
child.executableURL = URL(fileURLWithPath: project + "/.venv/bin/python")
child.arguments = [project + "/main.py"]
child.currentDirectoryURL = URL(fileURLWithPath: project)
child.standardOutput = log
child.standardError = log
var environment = ProcessInfo.processInfo.environment
// Started from Finder, PATH lacks Homebrew and the developer tools.
environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
environment["PYTHONUNBUFFERED"] = "1"
child.environment = environment

// Quitting the app (logout, Activity Monitor) quits Python too.
var sources: [DispatchSourceSignal] = []
for number in [SIGTERM, SIGINT, SIGHUP] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
    source.setEventHandler { child.interrupt() }
    source.resume()
    sources.append(source)
}

do {
    try child.run()
} catch {
    log.write("Could not start \(project)/.venv/bin/python: \(error)\n".data(using: .utf8)!)
    exit(1)
}
child.waitUntilExit()
exit(child.terminationStatus)
