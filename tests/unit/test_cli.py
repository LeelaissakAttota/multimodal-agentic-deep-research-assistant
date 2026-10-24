import base64
from pathlib import Path

import pytest

from deep_research.cli import attachment_for, build_parser, main


def test_parser_accepts_documented_flags() -> None:
    args = build_parser().parse_args(["run", "q?", "--file", "a.pdf", "--mode", "offline", "--export", "pdf", "--export", "md"])
    assert args.question == "q?" and args.file == ["a.pdf"] and args.mode == "offline" and args.export == ["pdf", "md"]
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "q", "--mode", "cloud"])


def test_attachment_for_local_files(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    att = attachment_for(str(pdf))
    assert att.kind == "pdf" and att.name == "paper.pdf"
    assert base64.b64decode(att.data.split(",", 1)[1]) == b"%PDF-1.4 test"
    with pytest.raises(SystemExit):
        attachment_for(str(tmp_path / "missing.pdf"))
    odd = tmp_path / "x.exe"
    odd.write_bytes(b"MZ")
    with pytest.raises(SystemExit):
        attachment_for(str(odd))


def test_cli_offline_run_prints_report_and_exports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["run", "Assess reliability of grid batteries", "--mode", "offline", "--export", "md", "--export", "json",
                 "--out", str(tmp_path)])
    out, err = capsys.readouterr()
    assert code == 0 and "[status: completed | mode: offline]" in err
    assert out.strip()
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".json", ".md"]
