// Feeds one or more processes' audio into the "OBS Auto Recorder Game Audio" loopback device
// (macos/game_audio_driver), where OBS records it like a microphone. This is for games macOS's
// per-app capture can't target: Wine games and Java Minecraft have no app bundle id.
//
// A Core Audio process tap (macOS 14.2+) captures processes by pid. OBS itself can't read a tap:
// macOS hands tap audio only to apps that declare NSAudioCaptureUsageDescription, and OBS doesn't
// (confirmed live: a tap OBS read directly recorded silence). So this helper reads it -- as part
// of OBS Auto Recorder, which declares it -- and plays it into the loopback device. Both sit in
// one private aggregate device clocked by the loopback device, so the tap is drift-compensated
// against it and a single IO callback copies tap input to loopback output.
//
// Usage: process_audio_tap --pid <pid> [--pid <pid> ...] [--device-uid <uid>] [--probe <seconds>]
// (--probe with no --pid taps all audio.)
// Prints "ready" once audio is flowing, then runs until stdin closes, SIGTERM/SIGINT, or every
// target process has exited. --probe reports the tap's loudest sample after that many seconds
// instead (a diagnostic, and a way to trigger the permission prompt).
import CoreAudio
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

var loopbackUID = "OBSAutoRecorderGameAudio_UID"
var pids: [pid_t] = []
var probeSeconds: Double?
var args = CommandLine.arguments.dropFirst().makeIterator()
while let arg = args.next() {
    switch arg {
    case "--pid": if let p = args.next().flatMap({ pid_t($0) }) { pids.append(p) }
    case "--device-uid": loopbackUID = args.next() ?? loopbackUID
    case "--probe": probeSeconds = args.next().flatMap { Double($0) }
    default: fail("unknown argument: \(arg)")
    }
}
if pids.isEmpty && probeSeconds == nil { fail("usage: process_audio_tap --pid <pid> [--pid <pid> ...] [--device-uid <uid>] [--probe <seconds>]") }

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
}

/// The Core Audio object for a pid, or nil until that process has opened audio at all.
func audioProcessObject(_ pid: pid_t) -> AudioObjectID? {
    var addr = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var qualifier = pid
    var object = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr,
        UInt32(MemoryLayout<pid_t>.size), &qualifier, &size, &object)
    return status == noErr && object != kAudioObjectUnknown ? object : nil
}

func deviceExists(uid: String) -> Bool {
    var addr = address(kAudioHardwarePropertyTranslateUIDToDevice)
    var qualifier = uid as CFString
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = withUnsafeMutablePointer(to: &qualifier) {
        AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr,
            UInt32(MemoryLayout<CFString>.size), $0, &size, &device)
    }
    return status == noErr && device != kAudioObjectUnknown
}

func defaultOutputUID() -> String? {
    var addr = address(kAudioHardwarePropertyDefaultSystemOutputDevice)
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &device) == noErr
    else { return nil }
    var uidAddr = address(kAudioDevicePropertyDeviceUID)
    var uid: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(device, &uidAddr, 0, nil, &size, &uid) == noErr else { return nil }
    return uid?.takeRetainedValue() as String?
}

func anyTargetAlive() -> Bool { pids.contains { kill($0, 0) == 0 || errno == EPERM } }

var stopping = false
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { stopping = true }
    source.resume()
    _ = Unmanaged.passRetained(source)
}
let probing = probeSeconds != nil
// A probe runs for its fixed time; otherwise stdin closing (the app quitting) stops the helper.
if !probing {
    FileHandle.standardInput.readabilityHandler = { handle in
        if handle.availableData.isEmpty { stopping = true; handle.readabilityHandler = nil }
    }
}
if !probing && !deviceExists(uid: loopbackUID) {
    fail("the Game Audio device isn't installed (\(loopbackUID))")
}

// A process only becomes tappable once it has opened audio; games often do that a few seconds
// after launch, so wait for it rather than failing.
var objects: [AudioObjectID] = []
while !pids.isEmpty {
    objects = pids.compactMap(audioProcessObject)
    if !objects.isEmpty { break }
    if stopping || !anyTargetAlive() { exit(0) }
    RunLoop.main.run(until: Date().addingTimeInterval(1))
}

// No pids (probe only): tap everything, which is enough to show the permission prompt.
let tapDescription = pids.isEmpty
    ? CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    : CATapDescription(stereoMixdownOfProcesses: objects)
tapDescription.uuid = UUID()
tapDescription.name = "OBS Auto Recorder Game Audio tap"
tapDescription.isPrivate = true
tapDescription.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
var status = AudioHardwareCreateProcessTap(tapDescription, &tapID)
if status != noErr { fail("could not create the process tap (\(status))") }

// Probing reads the tap only, clocked by the speakers; otherwise the loopback device is both the
// clock and the destination.
guard let clockUID = probing ? defaultOutputUID() : loopbackUID else { fail("no output device") }
let aggregate: [String: Any] = [
    kAudioAggregateDeviceUIDKey: "com.obsautorecorder.gameaudio.tap.\(getpid())",
    kAudioAggregateDeviceNameKey: "OBS Auto Recorder Game Audio tap",
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceMainSubDeviceKey: clockUID,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: clockUID]],
    kAudioAggregateDeviceTapListKey: [
        [kAudioSubTapUIDKey: tapDescription.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]
    ],
]
var aggregateID = AudioObjectID(kAudioObjectUnknown)
status = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID)
if status != noErr {
    AudioHardwareDestroyProcessTap(tapID)
    fail("could not create the tap device (\(status))")
}

// The tap's stream comes after the sub-device's own input streams, so it's the last input
// buffer; the loopback device's output is the only output buffer (the probe's speakers are
// left untouched).
var peak: Float = 0
var procID: AudioDeviceIOProcID?
status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { _, input, _, output, _ in
    let inputs = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
    let outputs = UnsafeMutableAudioBufferListPointer(output)
    guard let tap = inputs.last, let source = tap.mData else { return }
    if probing {
        let samples = source.assumingMemoryBound(to: Float.self)
        for i in 0..<Int(tap.mDataByteSize) / MemoryLayout<Float>.size { peak = max(peak, abs(samples[i])) }
        for buffer in outputs { if let data = buffer.mData { memset(data, 0, Int(buffer.mDataByteSize)) } }
        return
    }
    for buffer in outputs {
        guard let destination = buffer.mData else { continue }
        let count = Int(min(buffer.mDataByteSize, tap.mDataByteSize))
        memcpy(destination, source, count)
        if count < Int(buffer.mDataByteSize) {
            memset(destination + count, 0, Int(buffer.mDataByteSize) - count)
        }
    }
}
if status != noErr || AudioDeviceStart(aggregateID, procID) != noErr {
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
    fail("could not start audio (\(status))")
}

print("ready")
fflush(stdout)
let deadline = probeSeconds.map { Date().addingTimeInterval($0) }
while !stopping && (pids.isEmpty || anyTargetAlive()) {
    if let deadline, Date() >= deadline { break }
    RunLoop.main.run(until: Date().addingTimeInterval(0.5))
}
AudioDeviceStop(aggregateID, procID)
AudioDeviceDestroyIOProcID(aggregateID, procID!)
AudioHardwareDestroyAggregateDevice(aggregateID)
AudioHardwareDestroyProcessTap(tapID)
if probing { print(peak > 0 ? "probe peak dB: \(20 * log10(peak))" : "probe: silent") }
