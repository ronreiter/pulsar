import Foundation

/// The compiled song, as printed by `pulsar score`.
struct ScoreData: Decodable {
    let ok: Bool
    let tempo: Double?
    let key: String?
    let bar_seconds: Double?
    let total_bars: Double?
    let seconds: Double?
    let sections: [SectionData]?
    let tracks: [TrackData]?
    let automation: [AutoData]?
    let output: [String]?
    let error: ErrorData?
}

struct SectionData: Decodable, Identifiable, Hashable {
    var id: String { "\(name)@\(bar)" }
    let name: String
    let bar: Double
    let bars: Double
}

struct NoteData: Decodable, Hashable {
    let bar: Double
    let dur_bars: Double
    let pitch: Double?
    let drum: String?
    let vel: Double
    let slice: Int?
    let slide: Bool
    let accent: Bool
}

struct TrackData: Decodable, Identifiable, Hashable {
    var id: String { name }
    let name: String
    let lane: String  // notes | drums | sampler
    let preset: String
    let engine: String
    let effects: [String]
    let gain: Double
    let pan: Double
    let sends: [String: Double]
    let duck: Double
    let line: Int?
    let notes: [NoteData]
}

struct AutoData: Decodable, Identifiable, Hashable {
    var id: String { track + "." + param }
    let track: String
    let param: String
    let points: [[Double]]
}

struct ErrorData: Decodable, Hashable {
    let message: String
    let line: Int?
    let column: Int?
    let hint: String?
    let text: String
}
