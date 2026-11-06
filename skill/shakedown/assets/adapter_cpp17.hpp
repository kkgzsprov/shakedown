// C++17 bridge transport skeleton. Supply the game's JSON codec and input path.
// No JSON library is bundled; this file does not implement tape hashing or replay.
#pragma once

#include <functional>
#include <istream>
#include <ostream>
#include <set>
#include <stdexcept>
#include <string>

namespace shakedown_adapter {
struct Answer {
    int version;
    long long sequence;
    std::string choice;
};

// The callback must reject malformed JSON, non-integer v/seq and missing fields.
using ParseAnswer = std::function<Answer(const std::string&)>;

inline void emit(std::ostream& output, const std::string& encoded_object) {
    if (encoded_object.find('\n') != std::string::npos ||
        encoded_object.find('\r') != std::string::npos)
        throw std::runtime_error("one JSON object per wire line is required");
    output << "@@shakedown " << encoded_object << '\n' << std::flush;
    if (!output) throw std::runtime_error("wire output failed");
}

inline std::string bridge_choice(std::istream& input, std::ostream& output,
        const std::string& encoded_request, long long expected_sequence,
        const std::set<std::string>& legal_names, const ParseAnswer& parse) {
    // Pause simulation around this call. Send one hello before the first request.
    if (legal_names.empty() || legal_names.size() > 12)
        throw std::runtime_error("invalid candidates");
    emit(output, encoded_request);
    std::string line;
    if (!std::getline(input, line)) throw std::runtime_error("missing answer");
    // getline may return a final unterminated line; that is a contract error.
    if (input.eof()) throw std::runtime_error("answer missing newline");
    const auto answer = parse(line);
    if (answer.version != 1 || answer.sequence != expected_sequence ||
        legal_names.count(answer.choice) != 1)
        throw std::runtime_error("invalid answer");
    return answer.choice;
}
// Catch codec/contract errors at the game's process boundary and exit 2.
// Enqueue the selected input through the human input path on its application step.
// End with a flushed end object; oracle exit is 4. Tape divergence must exit 5.
}  // namespace shakedown_adapter
