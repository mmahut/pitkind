import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pitkind.cli import app
from pitkind.schemas import CanonicalFile

runner = CliRunner()

_CONSTITUTION = "Article I §3: Treasury withdrawals must include a detailed budget breakdown."

_SYSTEM_PROMPT = (
    "You are a constitutional reviewer for the Cardano blockchain. "
    "Assess the proposal against the Constitution and return a strict JSON object only."
)

_DELIBERATION_TEMPLATE = """\
Round: {round}

Proposal under review:
{proposal}

Your previous assessment:
{own_previous_output}

Peer assessments:
{peer_reviews}

Return a strict JSON object only."""

_PROPOSAL = {
    "id": "prop-test-001",
    "title": "Community Fund Allocation Q1",
    "type": "treasury_withdrawal",
    "body": "Request to withdraw 100,000 ADA for community development. Budget: infra 40k, grants 40k, admin 20k.",
}

_CONFIG = """\
models:
  - openrouter_id: "anthropic/claude-3-5-sonnet-20241022"
    decoding_params:
      temperature: 0
      top_p: 1.0
      max_tokens: 1024
  - openrouter_id: "openai/gpt-4o-2024-08-06"
    decoding_params:
      temperature: 0
      top_p: 1.0
      max_tokens: 1024
  - openrouter_id: "google/gemini-1.5-pro-latest"
    decoding_params:
      temperature: 0
      top_p: 1.0
      max_tokens: 1024
  - openrouter_id: "meta-llama/llama-3.1-405b-instruct"
    decoding_params:
      temperature: 0
      top_p: 1.0
      max_tokens: 1024
  - openrouter_id: "mistralai/mistral-large-2407"
    decoding_params:
      temperature: 0
      top_p: 1.0
      max_tokens: 1024

ballot_categories: ["YES", "NO", "ABSTAIN"]
max_retries: {max_retries}
max_api_retries: 3
output_dir: "{output_dir}"
"""


def _make_workspace(tmp_path, max_retries: int = 2):
    (tmp_path / "constitution.txt").write_text(_CONSTITUTION)
    (tmp_path / "system_prompt.txt").write_text(_SYSTEM_PROMPT)
    (tmp_path / "deliberation_template.txt").write_text(_DELIBERATION_TEMPLATE)
    (tmp_path / "proposal.json").write_text(json.dumps(_PROPOSAL))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (tmp_path / "config.yaml").write_text(
        _CONFIG.format(output_dir=str(output_dir), max_retries=max_retries)
    )
    return tmp_path


@pytest.fixture
def workspace(tmp_path):
    return _make_workspace(tmp_path)


def _run(workspace):
    return runner.invoke(app, [
        "run",
        "--constitution", str(workspace / "constitution.txt"),
        "--system-prompt", str(workspace / "system_prompt.txt"),
        "--deliberation-prompt-template", str(workspace / "deliberation_template.txt"),
        "--proposal", str(workspace / "proposal.json"),
        "--config", str(workspace / "config.yaml"),
        "--mock",
    ])


def test_mock_e2e_writes_one_file(workspace):
    result = _run(workspace)
    assert result.exit_code == 0, result.output
    output_files = list((workspace / "output").glob("*.json"))
    assert len(output_files) == 1
    checkpoints = list((workspace / "output").glob("*.events.jsonl"))
    assert len(checkpoints) == 1
    # One config-fingerprint header line plus one line per seat per round.
    assert len(checkpoints[0].read_text().splitlines()) == 16


def test_configured_ncl_is_passed_to_judging_and_fingerprinted(workspace, monkeypatch):
    import hashlib
    from pitkind import cli

    config_file = workspace / "config.yaml"
    config_file.write_text(config_file.read_text() +
                          "\nncl:\n  limit_lovelace: 42604371000000\n")
    messages = []
    original = cli._run_stages

    async def capture(*args, **kwargs):
        messages.append(kwargs["system_message"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(cli, "_run_stages", capture)
    result = _run(workspace)
    assert result.exit_code == 0, result.output
    data = json.loads(next((workspace / "output").glob("*.json")).read_text())
    fingerprint = data["config_fingerprint"]
    assert fingerprint["ncl"] == {"limit_lovelace": 42604371000000}
    assert "42604371000000" in messages[0]
    assert "do not invent a period or subtract historical spending" in messages[0]
    assert fingerprint["system_message_sha256"] == hashlib.sha256(messages[0].encode()).hexdigest()


def test_mock_e2e_canonical_file_valid(workspace):
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    data = json.loads(output_file.read_text())
    canonical = CanonicalFile.model_validate(data)
    assert canonical.pitkind_version == "v0.1"
    assert canonical.schema_version == "2.0"
    assert canonical.run_status == "complete"


def test_mock_e2e_three_stages_five_entries(workspace):
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    data = json.loads(output_file.read_text())
    assert len(data["stages"]["R0"]) == 5
    assert len(data["stages"]["R1"]) == 5
    assert len(data["stages"]["R2"]) == 5


def test_mock_e2e_official_verdict_binary(workspace):
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    data = json.loads(output_file.read_text())
    assert data["official_verdict"] in ("YES", "NO")
    assert data["summary"]
    assert len(data["summary"]) <= 300


def test_mock_e2e_config_fingerprint_present(workspace):
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    data = json.loads(output_file.read_text())
    fp = data["config_fingerprint"]
    assert len(fp["constitution_sha256"]) == 64
    assert len(fp["system_message_sha256"]) == 64
    assert fp["rounds"] == 2
    assert fp["aggregation_rule"] == "strict_majority_all_seats_else_abstain"
    assert fp["ballot_categories"] == ["YES", "NO", "ABSTAIN"]


def test_mock_e2e_user_messages_recorded(workspace):
    """Every stage entry records the exact user message the model received."""
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    data = json.loads(output_file.read_text())
    for stage_name, entries in data["stages"].items():
        for entry in entries:
            assert entry["user_message"], f"empty user_message in {stage_name}"
    r1_msg = data["stages"]["R1"][0]["user_message"]
    assert "Reviewer A" in r1_msg
    assert "prop-test-001" in r1_msg


def test_mock_e2e_schema_failure_recovers_via_retry(workspace):
    """Stub index 3 returns invalid JSON on first call; retries recover and the run completes."""
    result = _run(workspace)
    assert result.exit_code == 0, result.output
    output_file = next((workspace / "output").glob("*.json"))
    data = json.loads(output_file.read_text())
    assert data["run_status"] == "complete"
    assert data["stages"]["R0"][3]["output"]["verdict"] in ("YES", "NO")


def test_report_renders_html(workspace):
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    result = runner.invoke(app, ["report", str(output_file)])
    assert result.exit_code == 0, result.output
    html_file = output_file.with_suffix(".html")
    assert html_file.exists()
    html = html_file.read_text()
    data = json.loads(output_file.read_text())
    assert "anthropic/claude-3-5-sonnet-20241022" in html
    assert "Deliberation flow" in html
    assert data["summary"] in html
    assert "user_message" not in html  # bulky transcripts must not be embedded


def test_report_can_write_cip136_metadata(workspace):
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    result = runner.invoke(app, ["report", str(output_file), "--metadata"])
    assert result.exit_code == 0, result.output
    metadata = json.loads(output_file.with_suffix(".metadata.jsonld").read_text())
    assert len(metadata["body"]["summary"]) <= 300
    assert metadata["body"]["govActionId"] == "prop-test-001"
    assert metadata["body"]["references"][0]["uri"] == "https://deliberative.cc/prop-test-001"
    assert "Blake2b-256:" in result.output


@pytest.mark.parametrize("title", [
    "</SCRIPT><script>alert(1)</script>",
    "<!--<script>",
    "<!--<ScRiPt >",
])
def test_report_preserves_script_like_text_without_html_tokens(workspace, title):
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    data = json.loads(output_file.read_text())
    data["proposal"]["title"] = title
    output_file.write_text(json.dumps(data))
    result = runner.invoke(app, ["report", str(output_file)])
    assert result.exit_code == 0, result.output
    html = output_file.with_suffix(".html").read_text()
    payload = html.split('<script type="application/json" id="data">', 1)[1].split("</script>", 1)[0]
    # No HTML tokens may reach the script parser, including the comment/open-tag
    # sequence that closing-tag-only escaping leaves intact.
    assert "<" not in payload
    assert json.loads(payload)["proposal"]["title"] == title


def test_report_renders_failed_run(tmp_path):
    workspace = _make_workspace(tmp_path, max_retries=0)
    _run(workspace)
    output_file = next((workspace / "output").glob("*.json"))
    result = runner.invoke(app, ["report", str(output_file)])
    assert result.exit_code == 0, result.output
    html = output_file.with_suffix(".html").read_text()
    assert '"run_status": "failed"' in html


def test_mock_e2e_single_model_failure_fails_run(tmp_path):
    """With retries exhausted (max_retries=0), stub 3 fails R0 — the whole run must fail."""
    workspace = _make_workspace(tmp_path, max_retries=0)
    result = _run(workspace)
    assert result.exit_code == 1
    output_file = next((workspace / "output").glob("*.json"))
    data = json.loads(output_file.read_text())
    canonical = CanonicalFile.model_validate(data)
    assert canonical.run_status == "failed"
    assert canonical.official_verdict is None
    assert data["aggregations"]["R0"]["aggregation_status"] == "failed"
    assert "R1" not in data["stages"]
    assert "R2" not in data["stages"]
    assert data["stages"]["R0"][3]["output"]["status"] == "schema_failure"
