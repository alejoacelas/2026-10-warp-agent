// Print recognized text lines of an image as JSON: text, x, y, w, h in pixels (top-left origin).
import AppKit
import Foundation
import Vision

let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: url),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!)
    exit(1)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: cgImage).perform([request])
let width = Double(cgImage.width), height = Double(cgImage.height)
var lines: [[String: Any]] = []
for observation in request.results ?? [] {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let box = observation.boundingBox
    lines.append([
        "text": candidate.string,
        "x": Int(box.minX * width), "y": Int((1 - box.maxY) * height),
        "w": Int(box.width * width), "h": Int(box.height * height),
    ])
}
print(String(data: try JSONSerialization.data(withJSONObject: lines), encoding: .utf8)!)
