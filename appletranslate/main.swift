// Translates text with the built-in macOS translator (Apple's Translation framework):
// free, offline, and nothing leaves the Mac.
//
// Protocol: one JSON request per line on stdin, one JSON response per line on stdout.
//   in:  {"text": "hello", "source": "en", "target": "uk"}   // "source": "auto" means en
//   out: {"text": "привіт"}   or   {"error": "..."}
//
// The language pair has to be installed by the user first (System Settings ->
// General -> Language & Region -> Translation Languages): an app without a window
// cannot trigger the download.

import Foundation
import Translation

struct Request: Decodable {
    let text: String
    let source: String?
    let target: String?
}

func write(_ payload: [String: String]) {
    let data = (try? JSONSerialization.data(withJSONObject: payload)) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data + Data("\n".utf8))
}

var sessions: [String: TranslationSession] = [:]

// The framework wants a concrete source language; the caller knows it, because Whisper
// reports the language of every line it transcribes.
func session(source: String, target: String) -> TranslationSession {
    let key = "\(source)>\(target)"
    if let existing = sessions[key] {
        return existing
    }
    let created = TranslationSession(installedSource: Locale.Language(identifier: source),
                                     target: Locale.Language(identifier: target))
    sessions[key] = created
    return created
}

// A ready line tells the caller the helper is up, so it can report a clear startup error.
write(["ready": "1"])

while let line = readLine(strippingNewline: true) {
    guard !line.isEmpty, let data = line.data(using: .utf8),
          let request = try? JSONDecoder().decode(Request.self, from: data) else {
        write(["error": "bad request"])
        continue
    }
    guard let target = request.target, !target.isEmpty else {
        write(["error": "no target language"])
        continue
    }
    let source = request.source.flatMap { $0 == "auto" || $0.isEmpty ? nil : $0 } ?? "en"
    do {
        let response = try await session(source: source, target: target).translate(request.text)
        write(["text": response.targetText])
    } catch {
        // notInstalled is the common one: the user has to add the language pair in System Settings.
        // Drop the session so the next request starts a fresh one instead of reusing a failed one.
        sessions["\(source)>\(target)"] = nil
        write(["error": "\(error)"])
    }
}
