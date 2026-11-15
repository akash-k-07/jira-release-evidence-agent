from release_agent.textutil import adf_to_text, clip, labelled_value, parse_recorded_event, split_summary, text_to_adf


def test_adf_round_trip_preserves_lines():
    text = "Line one\n\nLine | three with \"quotes\""
    assert adf_to_text(text_to_adf(text)) == text


def test_parse_recorded_event():
    parsed = parse_recorded_event("Recorded event 2026-11-08T15:00:00+05:30 | actor Asha Rao | role Security Owner\nApproved: staging.")
    assert parsed == {"recorded_at": "2026-11-08T15:00:00+05:30", "actor": "Asha Rao", "role": "Security Owner", "body": "Approved: staging."}


def test_parse_recorded_event_rejects_free_text():
    assert parse_recorded_event("Just a note") is None


def test_split_summary_and_labelled_value():
    assert split_summary("[REQ-04] Response latency") == ("REQ-04", "Response latency")
    assert split_summary("No prefix") == (None, "No prefix")
    assert labelled_value("Mandatory: yes\nAcceptance criterion: p95 <= 2.0 s", "Acceptance criterion") == "p95 <= 2.0 s"


def test_clip():
    assert clip("abc", 5) == "abc"
    assert clip("abcdef", 3).startswith("abc... [truncated 3")
