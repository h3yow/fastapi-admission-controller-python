import logging
import sys

import pytest

from controlplane.admission import AdmissionController
from controlplane.store import ResourceStore

# 单测日志：打印输入与判定依据，便于从日志追溯每个断言的理由。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("controlplane.test")


@pytest.fixture
def store() -> ResourceStore:
    return ResourceStore(AdmissionController(allowed_namespaces={"team-a", "team-b"}))


def log_case(action: str, inputs: dict, basis: str) -> None:
    """打印测试输入与判定依据。"""
    logger.info("CASE %s | input=%s | 判定依据: %s", action, inputs, basis)
