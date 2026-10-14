import json
import sys
from pathlib import Path

import pytest

from bookkeeping.audit import Audit
from bookkeeping.config import Settings
from bookkeeping.journal import Journal
from bookkeeping.knowledge import Knowledge

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        lark_app_id="cli_test",
        lark_app_secret="test-secret-do-not-log",
        ark_api_key="test-key-do-not-log",
        ark_model="test-model",
        bitable_base_token="test-base-do-not-log",
        bitable_table_id="tblTestLedger",
        lark_cli_path=sys.executable,
        project_dir=tmp_path,
        runtime_dir=tmp_path,
    )


@pytest.fixture
def audit(settings):
    instance = Audit(settings.private_dir / "events.jsonl", settings.secrets)
    yield instance
    instance.close()


@pytest.fixture
def journal(tmp_path):
    instance = Journal(tmp_path / "journal.sqlite3", "unit-test")
    yield instance
    instance.close()


@pytest.fixture
def knowledge(settings):
    # Entirely synthetic data; public tests must not require anyone's private knowledge.
    folder = settings.project_dir / "knowledges"
    folder.mkdir()
    categories = {
        "version": "test.1",
        "table_id": settings.bitable_table_id,
        "tree": [
            {
                "name": "饮食",
                "children": [
                    {"name": "咖啡", "children": []},
                    {"name": "饮用水", "children": []},
                ],
            },
            {
                "name": "交通通信",
                "children": [
                    {"name": "汽车", "children": [{"name": "停车费", "children": []}]},
                ],
            },
            {"name": "测试类别", "children": [{"name": "测试二级", "children": []}]},
        ],
    }
    rules = {
        "version": "test.1",
        "compatible_categories_version": "test.1",
        "rules": [
            {"id": "TEST_COFFEE", "category": ["饮食", "咖啡", None]},
            {"id": "TEST_WATER", "category": ["饮食", "饮用水", None]},
            {"id": "TEST_PARKING", "category": ["交通通信", "汽车", "停车费"]},
            {"id": "TEST_OTHER", "category": ["测试类别", "测试二级", None]},
        ],
    }
    (folder / "categories.json").write_text(json.dumps(categories, ensure_ascii=False))
    (folder / "rules.json").write_text(json.dumps(rules, ensure_ascii=False))
    return Knowledge(settings.project_dir, settings.bitable_table_id)


@pytest.fixture
def private_knowledge():
    folder = ROOT / "knowledges"
    if not all((folder / name).is_file() for name in ["categories.json", "rules.json"]):
        pytest.skip("本地未提供个人知识库，跳过私有样本回放")
    categories = json.loads((folder / "categories.json").read_text(encoding="utf-8"))
    return Knowledge(ROOT, categories["table_id"])


@pytest.fixture
def private_replay():
    path = ROOT / "knowledges" / "testing" / "real_model_classification.json"
    if not path.is_file():
        pytest.skip("本地未提供真实模型样本，跳过私有样本回放")
    return json.loads(path.read_text(encoding="utf-8"))
