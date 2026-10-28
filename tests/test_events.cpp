#include <catch2/catch_test_macros.hpp>

#include <nlohmann/json.hpp>
#include <string>
#include <vector>

#include "watashi/events.hpp"

namespace {

watashi::Envelope sample() {
    return watashi::encode_event(
        watashi::event_type::kSubtitle,
        nlohmann::json{
            {"source", "the sword intent"},
            {"target", "jian yi"},
            {"lines", nlohmann::json::array()},
        },
        3,
        1700000000.5);
}

}  // namespace

TEST_CASE("an envelope round-trips through JSON") {
    const auto original = sample();
    const auto decoded = watashi::decode_envelope(watashi::to_json(original));

    REQUIRE(decoded.v == watashi::kSchemaVersion);
    REQUIRE(decoded.type == "subtitle");
    REQUIRE(decoded.ts == original.ts);
    REQUIRE(decoded.seq == 3);
    REQUIRE(decoded.data == original.data);
}

TEST_CASE("the encoded envelope has exactly the five contract keys") {
    const auto document = watashi::to_json(sample());

    REQUIRE(document.size() == 5);
    REQUIRE(document.contains("v"));
    REQUIRE(document.contains("type"));
    REQUIRE(document.contains("ts"));
    REQUIRE(document.contains("seq"));
    REQUIRE(document.contains("data"));
}

TEST_CASE("to_json_line is a single JSON line") {
    const std::string line = watashi::to_json_line(sample());

    REQUIRE(line.find('\n') == std::string::npos);
    const auto parsed = nlohmann::json::parse(line);
    REQUIRE(parsed.at("type") == "subtitle");
    REQUIRE(parsed.at("seq") == 3);
}

TEST_CASE("encode_event rejects an unknown event type") {
    REQUIRE_THROWS_AS(
        watashi::encode_event("caption", nlohmann::json::object(), 1, 0.0),
        watashi::ContractError);
}

TEST_CASE("encode_event rejects a non-positive seq") {
    REQUIRE_THROWS_AS(
        watashi::encode_event(watashi::event_type::kReady, nlohmann::json::object(), 0, 0.0),
        watashi::ContractError);
}

TEST_CASE("decode_envelope rejects a non-object") {
    REQUIRE_THROWS_AS(
        watashi::decode_envelope(nlohmann::json::array()),
        watashi::ContractError);
}

TEST_CASE("decode_envelope rejects another schema version") {
    auto document = watashi::to_json(sample());
    document["v"] = 2;

    REQUIRE_THROWS_AS(watashi::decode_envelope(document), watashi::ContractError);
}

TEST_CASE("decode_envelope rejects an unknown event type") {
    auto document = watashi::to_json(sample());
    document["type"] = "caption";

    REQUIRE_THROWS_AS(watashi::decode_envelope(document), watashi::ContractError);
}

TEST_CASE("decode_envelope rejects a missing ts") {
    auto document = watashi::to_json(sample());
    document.erase("ts");

    REQUIRE_THROWS_AS(watashi::decode_envelope(document), watashi::ContractError);
}

TEST_CASE("decode_envelope rejects non-object data") {
    auto document = watashi::to_json(sample());
    document["data"] = nlohmann::json::array();

    REQUIRE_THROWS_AS(watashi::decode_envelope(document), watashi::ContractError);
}

TEST_CASE("decode_envelope rejects seq below one") {
    auto document = watashi::to_json(sample());
    document["seq"] = 0;

    REQUIRE_THROWS_AS(watashi::decode_envelope(document), watashi::ContractError);
}

TEST_CASE("decode_envelope accepts an envelope without seq") {
    auto document = watashi::to_json(sample());
    document.erase("seq");

    const auto decoded = watashi::decode_envelope(document);
    REQUIRE(decoded.seq == 0);
    REQUIRE(decoded.type == "subtitle");
}

TEST_CASE("the schema version stays one") {
    REQUIRE(watashi::kSchemaVersion == 1);
    REQUIRE(watashi::decode_envelope(watashi::to_json(sample())).v == 1);
}

TEST_CASE("every Python event name is known") {
    const std::vector<std::string> names = {
        "ready",   "subtitle", "refinement",   "stats",       "status", "error",
        "settings", "correction", "library",    "command_result", "stopped",
        "presentation",
    };
    for (const auto& name : names) {
        REQUIRE(watashi::is_known_event(name));
    }
    REQUIRE_FALSE(watashi::is_known_event("caption"));
}
