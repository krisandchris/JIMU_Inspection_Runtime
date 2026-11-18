"""Fresh stage1 fixture copies; no synthesized Producer or admission substitute."""

import json
from pathlib import Path

from jimu_runtime.domain.validation.producer_authority_inputs import (
    ProducerAuthorityValidationInput,
)


def fixture():
    return json.loads(Path(__file__).with_name("inputs.json").read_text())["input"]


def parse(data):
    # Stage1 uses an empty candidate snapshot, so no bytes transport convention
    # is invented by this JSON fixture. Later integration supplies actual bytes.
    return ProducerAuthorityValidationInput.model_validate_json(json.dumps(data))
