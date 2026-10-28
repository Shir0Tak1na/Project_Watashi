#include "watashi/events.hpp"

#include <ostream>
#include <string>

namespace watashi {

std::string to_json_line(const Envelope& envelope) {
    // One JSON object per line, UTF-8, no pretty printing: NDJSON is what
    // `--json-lines` writes and what tools/validate_stream.py reads back.
    return to_json(envelope).dump();
}

void write_json_line(std::ostream& out, const Envelope& envelope) {
    out << to_json_line(envelope) << '\n';
}

}  // namespace watashi
