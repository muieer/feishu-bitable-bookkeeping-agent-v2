"""Attribute the same-round report contradiction to an extracted baseline.

This diagnostic intentionally asserts that the contradiction exists. The actual
business acceptance assertion stays failing in acceptance_unique_id.py.
"""

import argparse
import asyncio
import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def probe(args):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(args.source.resolve()))
    import pytest

    import bookkeeping
    from tests.acceptance_unique_id import AcceptanceRig, add_turn, envelope, finish_turn, proposal

    imported = Path(bookkeeping.__file__).resolve()
    assert imported.parent == args.source.resolve() / "bookkeeping"
    attempted = []

    def blocked(*values, **kwargs):
        attempted.append(True)
        raise AssertionError("Baseline attribution must remain offline")

    with TemporaryDirectory(prefix="independent-baseline-") as temporary:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(socket.socket, "connect", blocked)
            patch.setattr(socket.socket, "connect_ex", blocked)
            patch.setattr(socket, "getaddrinfo", blocked)
            patch.setattr(subprocess, "Popen", blocked)
            patch.setattr(asyncio, "create_subprocess_exec", blocked)
            rig = AcceptanceRig(Path(temporary), patch)
            try:

                async def accept_at_process_boundary(action, argv):
                    assert action == "create" and argv[:2] == ["base", "+record-batch-create"]
                    rig.boundary.trace.append({"action": action, "argv": argv})
                    return envelope({"record_id_list": ["recBaselineSynthetic"]})

                patch.setattr(rig.boundary, "run", accept_at_process_boundary)
                raw = "昨天乘地铁花了3元"
                turn = add_turn(proposal()) + finish_turn(
                    skipped=[{"source_text": raw, "reason": "金额暂时不明确，未提交"}]
                )
                report = asyncio.run(rig.run(raw, [turn, finish_turn()]))
                assert len(report.entries) == 1 and report.entries[0].outcome.status == "success"
                assert len(report.skipped) == 1 and report.skipped[0].source_text == raw
                assert rig.model.requests == 1 and len(rig.boundary.trace) == 1
                assert not attempted
                result = {
                    "label": args.label,
                    "imported_source": str(imported),
                    "source_sha256": {
                        name: hashlib.sha256((imported.parent / name).read_bytes()).hexdigest()
                        for name in ["agent.py", "service.py", "tools.py", "cards.py"]
                    },
                    "model_requests": rig.model.requests,
                    "report": report.model_dump(mode="json"),
                    "final_cards": rig.channel.finals,
                    "cli_trace": rig.boundary.trace,
                    "conclusion": "same successful source is also skipped",
                    "external_attempts": 0,
                }
                args.output.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    json.dumps(
                        {
                            "label": args.label,
                            "imported_source": str(imported),
                            "baseline_contradiction_reproduced": True,
                        },
                        ensure_ascii=False,
                    )
                )
            finally:
                rig.journal.close()
                rig.audit.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    probe(parser.parse_args())
