// bmcapture — beyondMeetings capture helper for macOS.
//
// Records system audio and the microphone to two separate WAV files. Two
// files because macOS 13 and 14 cannot deliver both from one API:
// SCStreamConfiguration.captureMicrophone arrived in macOS 15. The Python side
// mixes them with ffmpeg when a segment closes.
//
// Usage:
//   bmcapture record --system out.wav [--mic out.wav] (records until SIGTERM)
//   bmcapture check-permissions --json
//   bmcapture list-devices --json
//
// Build:
//   swiftc -O bmcapture.swift -o bmcapture \
//     -framework ScreenCaptureKit -framework AVFoundation -framework CoreMedia
//
// NOT YET VERIFIED ON HARDWARE. This was written without a Mac to compile or
// run it on. Treat the first build as a debugging session, not a formality.

import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

// MARK: - WAV writing

/// Writes CMSampleBuffers to a WAV file, deriving the file format from the
/// first buffer that arrives. The format is not known until then, which is why
/// the file is created lazily rather than in init.
final class AudioFileWriter {
    private let url: URL
    private var file: AVAudioFile?
    private let queue = DispatchQueue(label: "bmcapture.writer")

    init(url: URL) {
        self.url = url
    }

    func write(_ sampleBuffer: CMSampleBuffer) {
        queue.sync {
            guard let description = CMSampleBufferGetFormatDescription(sampleBuffer),
                  let streamBasicDescription =
                    CMAudioFormatDescriptionGetStreamBasicDescription(description)
            else { return }

            guard let format = AVAudioFormat(
                streamDescription: streamBasicDescription
            ) else { return }

            if file == nil {
                // Write interleaved 16-bit PCM regardless of the source format:
                // it is what every consumer downstream expects from a .wav.
                let settings: [String: Any] = [
                    AVFormatIDKey: kAudioFormatLinearPCM,
                    AVSampleRateKey: format.sampleRate,
                    AVNumberOfChannelsKey: format.channelCount,
                    AVLinearPCMBitDepthKey: 16,
                    AVLinearPCMIsFloatKey: false,
                    AVLinearPCMIsBigEndianKey: false,
                    AVLinearPCMIsNonInterleaved: false,
                ]
                file = try? AVAudioFile(forWriting: url, settings: settings)
            }
            guard let file else { return }

            guard let buffer = Self.pcmBuffer(from: sampleBuffer, format: format)
            else { return }

            try? file.write(from: buffer)
        }
    }

    /// Closing the file object is what finalizes the WAV header. Without this a
    /// SIGTERM leaves a file whose header claims zero frames.
    func close() {
        queue.sync { file = nil }
    }

    private static func pcmBuffer(
        from sampleBuffer: CMSampleBuffer, format: AVAudioFormat
    ) -> AVAudioPCMBuffer? {
        let frames = CMSampleBufferGetNumSamples(sampleBuffer)
        guard frames > 0,
              let buffer = AVAudioPCMBuffer(
                pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)
              )
        else { return nil }
        buffer.frameLength = AVAudioFrameCount(frames)

        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer,
            at: 0,
            frameCount: Int32(frames),
            into: buffer.mutableAudioBufferList
        )
        return status == noErr ? buffer : nil
    }
}

// MARK: - System audio via ScreenCaptureKit

/// Audio-only capture. ScreenCaptureKit has no audio-only mode, so a filter
/// over a display is still required — the video stream is configured as small
/// as possible and its frames are discarded.
@available(macOS 13.0, *)
final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let writer: AudioFileWriter
    private var stream: SCStream?

    init(writer: AudioFileWriter) {
        self.writer = writer
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false
        )
        guard let display = content.displays.first else {
            throw CaptureError.noDisplay
        }

        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 2
        // Excludes our own process so the helper cannot record itself.
        configuration.excludesCurrentProcessAudio = true
        // Video cannot be switched off; make it as cheap as possible.
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 5

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let stream = SCStream(
            filter: filter, configuration: configuration, delegate: self
        )
        try stream.addStreamOutput(
            self,
            type: .audio,
            sampleHandlerQueue: DispatchQueue(label: "bmcapture.system")
        )
        try await stream.startCapture()
        self.stream = stream
    }

    func stop() async {
        try? await stream?.stopCapture()
        stream = nil
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        writer.write(sampleBuffer)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write(
            "bmcapture: system audio stream stopped: \(error)\n".data(using: .utf8)!
        )
    }
}

// MARK: - Microphone via AVFoundation

/// SCStreamConfiguration.captureMicrophone is macOS 15+, so at our floor the
/// microphone is a separate AVCaptureSession.
final class MicrophoneCapture: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    private let writer: AudioFileWriter
    private let session = AVCaptureSession()

    init(writer: AudioFileWriter) {
        self.writer = writer
    }

    func start() throws {
        guard let device = AVCaptureDevice.default(for: .audio) else {
            throw CaptureError.noMicrophone
        }
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else { throw CaptureError.noMicrophone }
        session.addInput(input)

        let output = AVCaptureAudioDataOutput()
        output.setSampleBufferDelegate(
            self, queue: DispatchQueue(label: "bmcapture.mic")
        )
        guard session.canAddOutput(output) else { throw CaptureError.noMicrophone }
        session.addOutput(output)

        session.startRunning()
    }

    func stop() {
        session.stopRunning()
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }
        writer.write(sampleBuffer)
    }
}

enum CaptureError: Error, CustomStringConvertible {
    case noDisplay
    case noMicrophone
    case badArguments(String)

    var description: String {
        switch self {
        case .noDisplay:
            return "no display available to attach an audio capture to"
        case .noMicrophone:
            return "no usable microphone"
        case .badArguments(let detail):
            return detail
        }
    }
}

// MARK: - Permissions

func hasScreenRecordingPermission() -> Bool {
    // Preflight does not prompt; it only reports.
    CGPreflightScreenCaptureAccess()
}

func hasMicrophonePermission() -> Bool {
    AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
}

/// Triggers the system prompts. Screen recording additionally requires the app
/// to be relaunched before a granted permission takes effect.
func requestPermissions() async {
    if !hasScreenRecordingPermission() {
        CGRequestScreenCaptureAccess()
    }
    if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
        _ = await AVCaptureDevice.requestAccess(for: .audio)
    }
}

// MARK: - Argument parsing

func value(of flag: String, in args: [String]) throws -> String {
    guard let index = args.firstIndex(of: flag), index + 1 < args.count else {
        throw CaptureError.badArguments("missing value for \(flag)")
    }
    return args[index + 1]
}

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(
        withJSONObject: object, options: [.sortedKeys, .prettyPrinted]
    ) else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("bmcapture: \(message)\n".data(using: .utf8)!)
    exit(1)
}

// MARK: - Commands

@available(macOS 13.0, *)
func record(systemPath: String, micPath: String?) async {
    let systemWriter = AudioFileWriter(
        url: URL(fileURLWithPath: systemPath)
    )
    let micWriter = micPath.map {
        AudioFileWriter(url: URL(fileURLWithPath: $0))
    }

    let system = SystemAudioCapture(writer: systemWriter)
    let mic = micWriter.map { MicrophoneCapture(writer: $0) }

    do {
        try await system.start()
    } catch {
        fail("could not start system audio capture: \(error)")
    }

    // A missing or denied microphone must not abort the recording — the
    // system audio is the more important of the two streams, and the Python
    // side already tolerates a missing mic file.
    if let mic {
        do {
            try mic.start()
        } catch {
            FileHandle.standardError.write(
                "bmcapture: microphone unavailable, continuing without it: \(error)\n"
                    .data(using: .utf8)!
            )
        }
    }

    // SIGTERM is how the Python side ends a segment. The default disposition
    // would kill the process before the WAV headers were finalized, losing the
    // segment, so it is handled explicitly.
    let stopping = DispatchSemaphore(value: 0)
    let source = DispatchSource.makeSignalSource(
        signal: SIGTERM, queue: .global()
    )
    source.setEventHandler { stopping.signal() }
    source.resume()
    signal(SIGTERM, SIG_IGN)

    stopping.wait()

    await system.stop()
    mic?.stop()
    systemWriter.close()
    micWriter?.close()
}

func checkPermissions() {
    emit([
        "screen_recording": hasScreenRecordingPermission(),
        "microphone": hasMicrophonePermission(),
    ])
}

func listDevices() {
    let session = AVCaptureDevice.DiscoverySession(
        deviceTypes: [.microphone],
        mediaType: .audio,
        position: .unspecified
    )
    emit([
        "microphones": session.devices.map {
            ["id": $0.uniqueID, "name": $0.localizedName]
        }
    ])
}

// MARK: - Entry point

let arguments = Array(CommandLine.arguments.dropFirst())
guard let command = arguments.first else {
    fail("usage: bmcapture record --system PATH [--mic PATH] | check-permissions | list-devices")
}

guard #available(macOS 13.0, *) else {
    fail("macOS 13 or later is required (ScreenCaptureKit audio capture)")
}

switch command {
case "record":
    do {
        let systemPath = try value(of: "--system", in: arguments)
        let micPath: String?
        if arguments.contains("--mic") {
            micPath = try value(of: "--mic", in: arguments)
        } else {
            micPath = nil
        }
        let done = DispatchSemaphore(value: 0)
        Task {
            await record(systemPath: systemPath, micPath: micPath)
            done.signal()
        }
        done.wait()
    } catch {
        fail("\(error)")
    }

case "check-permissions":
    checkPermissions()

case "request-permissions":
    let done = DispatchSemaphore(value: 0)
    Task {
        await requestPermissions()
        done.signal()
    }
    done.wait()
    checkPermissions()

case "list-devices":
    listDevices()

default:
    fail("unknown command: \(command)")
}
