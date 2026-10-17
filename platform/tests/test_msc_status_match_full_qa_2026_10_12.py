"""Tests for the one-file full local-QA command entry point."""

import json

from MSC_Status_Match_Toolkit import msc_status_match_full_qa as script


def test_full_qa_script_dispatches_all_cli_options(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fake_workflow(**kwargs):
        calls.update(kwargs)
        return {
            "status": "completed_local_qa_not_submitted",
            "form_submitted": False,
        }

    monkeypatch.setattr(script, "run_end_to_end_local_qa", fake_workflow)

    exit_code = script.main([
        "--booking",
        "3001061",
        "--passenger-index",
        "1",
        "--state",
        "Oklahoma",
        "--output-dir",
        str(tmp_path / "private"),
        "--inbox-wait-seconds",
        "0",
        "--login-timeout-minutes",
        "0",
    ])

    assert exit_code == 0
    assert calls == {
        "booking_id": "3001061",
        "passenger_index": 1,
        "state": "Oklahoma",
        "output_dir": tmp_path / "private",
        "inbox_wait_seconds": 0,
        "login_timeout_minutes": 0.0,
    }
    assert json.loads(capsys.readouterr().out)["form_submitted"] is False


def test_full_qa_script_returns_failure_when_workflow_is_incomplete(
    monkeypatch, capsys, tmp_path
):
    async def failed_workflow(**_kwargs):
        return {"status": "failed", "form_submitted": False}

    monkeypatch.setattr(script, "run_end_to_end_local_qa", failed_workflow)

    exit_code = script.main([
        "--booking",
        "3001061",
        "--passenger-index",
        "1",
        "--output-dir",
        str(tmp_path / "private"),
    ])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
