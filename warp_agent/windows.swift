// Print Warp's normal windows as JSON: id, title, onscreen.
import CoreGraphics
import Foundation

let all = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []
var windows: [[String: Any]] = []
for window in all {
    guard (window[kCGWindowOwnerName as String] as? String) == "Warp",
          (window[kCGWindowLayer as String] as? Int) == 0,
          let title = window[kCGWindowName as String] as? String, !title.isEmpty else { continue }
    windows.append([
        "id": window[kCGWindowNumber as String] as? Int ?? 0,
        "title": title,
        "onscreen": window[kCGWindowIsOnscreen as String] as? Bool ?? false,
    ])
}
print(String(data: try JSONSerialization.data(withJSONObject: windows), encoding: .utf8)!)
