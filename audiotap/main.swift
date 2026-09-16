// Captures the audio that goes to the speakers (a Core Audio process tap, macOS 14.2+)
// and writes it to stdout as mono float32, so no BlackHole / Multi-Output Device is needed
// and the volume keys keep working.
//
// Protocol: one JSON header line ({"rate": 48000}), then raw float32 samples.

import AudioToolbox
import CoreAudio
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func check(_ status: OSStatus, _ what: String) {
    if status != noErr {
        fail("\(what) failed: OSStatus \(status)")
    }
}

func defaultOutputDeviceUID() -> String {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var device = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device),
          "reading the default output device")

    address.mSelector = kAudioDevicePropertyDeviceUID
    var uid: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    check(AudioObjectGetPropertyData(device, &address, 0, nil, &size, &uid), "reading the output device UID")
    guard let uid else { fail("the output device has no UID") }
    return uid.takeRetainedValue() as String
}

// 1. Tap everything the system plays, without muting it.
let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDescription.name = "transwhisper"
tapDescription.isPrivate = true
tapDescription.muteBehavior = .unmuted
var tap = AudioObjectID(kAudioObjectUnknown)
let tapStatus = AudioHardwareCreateProcessTap(tapDescription, &tap)
if tapStatus != noErr {
    fail("""
    Could not start capturing system audio: OSStatus \(tapStatus).
    macOS asks for permission the first time: System Settings -> Privacy & Security -> \
    Screen & System Audio Recording -> allow your terminal, then run again.
    """)
}

// 2. A private aggregate device that carries the tap's audio as its input.
let outputUID = defaultOutputDeviceUID()
let aggregateUID = "transwhisper-tap-\(UUID().uuidString)"
let description: [String: Any] = [
    kAudioAggregateDeviceNameKey: "transwhisper tap",
    kAudioAggregateDeviceUIDKey: aggregateUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceMainSubDeviceKey: outputUID,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapUIDKey: tapDescription.uuid.uuidString,
        kAudioSubTapDriftCompensationKey: true,
    ]],
]
var aggregate = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregate), "creating the capture device")

var formatAddress = AudioObjectPropertyAddress(
    mSelector: kAudioDevicePropertyStreamFormat,
    mScope: kAudioObjectPropertyScopeInput,
    mElement: kAudioObjectPropertyElementMain)
var format = AudioStreamBasicDescription()
var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
check(AudioObjectGetPropertyData(aggregate, &formatAddress, 0, nil, &formatSize, &format), "reading the capture format")

let output = FileHandle.standardOutput
output.write(Data("{\"rate\": \(Int(format.mSampleRate))}\n".utf8))

// 3. Downmix to mono and write raw float32 samples.
let debug = CommandLine.arguments.contains("--debug")
nonisolated(unsafe) var callbacks = 0
nonisolated(unsafe) var peak: Float = 0
var procID: AudioDeviceIOProcID?
let status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregate, nil) { _, inInput, _, _, _ in
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInput))
    guard let first = buffers.first, first.mDataByteSize > 0 else { return }

    let frames = Int(first.mDataByteSize) / MemoryLayout<Float>.size / Int(max(first.mNumberChannels, 1))
    var mono = [Float](repeating: 0, count: frames)
    var sources = 0
    for buffer in buffers {
        guard let data = buffer.mData else { continue }
        let channels = Int(max(buffer.mNumberChannels, 1))
        let samples = data.assumingMemoryBound(to: Float.self)
        for frame in 0..<frames {
            var sum: Float = 0
            for channel in 0..<channels {
                sum += samples[frame * channels + channel]
            }
            mono[frame] += sum / Float(channels)
        }
        sources += 1
    }
    if sources > 1 {
        for frame in 0..<frames { mono[frame] /= Float(sources) }
    }
    mono.withUnsafeBytes { output.write(Data($0)) }

    if debug {
        callbacks += 1
        peak = max(peak, mono.map(abs).max() ?? 0)
        if callbacks % 50 == 0 {
            FileHandle.standardError.write(Data("callbacks \(callbacks), frames \(frames), buffers \(buffers.count), peak \(peak)\n".utf8))
            peak = 0
        }
    }
}
check(status, "installing the capture callback")

func cleanUp() {
    if let procID { AudioDeviceStop(aggregate, procID) }
    AudioHardwareDestroyAggregateDevice(aggregate)
    AudioHardwareDestroyProcessTap(tap)
}
for signalNumber in [SIGINT, SIGTERM, SIGPIPE] {
    signal(signalNumber, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .main)
    source.setEventHandler { cleanUp(); exit(0) }
    source.resume()
}

check(AudioDeviceStart(aggregate, procID), "starting capture")
atexit(cleanUp)
CFRunLoopRun()
