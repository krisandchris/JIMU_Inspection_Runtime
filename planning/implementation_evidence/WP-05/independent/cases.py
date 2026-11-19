"""Independent test stimuli. Expected workflow outcomes live in gold.json."""

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures")
BASE_FRAME_DIGEST = "f837e12b86350de02e80e928f939bc68a2f56a2d5bdab421dde257166ce2bdcb"
type Box = tuple[int, int, int, int]
OUTSIDE: Box = (40, 23, 52, 33)
INSIDE: Box = (18, 18, 30, 28)
OUT_OF_BOUNDS: Box = (40, 23, 65, 33)
QUESTION_IDS = frozenset({"vehicle_type", "parking_state", "parking_position"})
CATEGORY = "nonmotor_vehicle_illegal_parking"
PROFILE = "nonmotor_vehicle"
PARKED = (
    ("vehicle_type", "electric_bicycle"),
    ("parking_state", "parked"),
    ("parking_position", "outside_parking_area"),
)
UNPARKED = (
    ("vehicle_type", "bicycle"),
    ("parking_state", "unparked"),
    ("parking_position", "inside_parking_area"),
)


@dataclass(frozen=True, slots=True)
class ModelStimulus:
    hits: tuple[str, ...] = (PROFILE,)
    boxes: tuple[Box, ...] = (OUTSIDE,)
    subjects: tuple[tuple[Box, tuple[tuple[str, str], ...]], ...] = ((OUTSIDE, PARKED),)
    first_batch_extra_slot: bool = False
    delay_grounding: bool = False
    candidate_unavailable: bool = False
    delay_candidate: bool = False
    image_variant: str = "base"

    def question_reply(self, box: Box) -> dict[str, str]:
        return dict(dict(self.subjects)[box])


CASES = {
    "known_violation": ModelStimulus(),
    "no_hits": ModelStimulus(hits=(), boxes=(), subjects=()),
    "no_target": ModelStimulus(boxes=(), subjects=()),
    "grounding_failure": ModelStimulus(boxes=(OUT_OF_BOUNDS,), subjects=()),
    "grounding_timeout": ModelStimulus(boxes=(), subjects=(), delay_grounding=True),
    "candidate_not_configured": ModelStimulus(),
    "candidate_unavailable": ModelStimulus(candidate_unavailable=True),
    "candidate_timeout": ModelStimulus(delay_candidate=True),
    "missing_slot": ModelStimulus(subjects=((OUTSIDE, PARKED[:-1]),)),
    "explicit_visibility": ModelStimulus(
        subjects=(
            (OUTSIDE, (*PARKED[:-1], ("parking_position", "insufficient_visibility"))),
        )
    ),
    "whole_batch_retry": ModelStimulus(first_batch_extra_slot=True),
    "wheel_on_tactile": ModelStimulus(
        boxes=(INSIDE,),
        subjects=(
            (INSIDE, (*PARKED[:-1], ("parking_position", "inside_parking_area"))),
        ),
        image_variant="wheel-on-tactile",
    ),
    "multiple_objects": ModelStimulus(
        boxes=(OUTSIDE, INSIDE),
        subjects=((OUTSIDE, PARKED), (INSIDE, UNPARKED)),
        image_variant="two",
    ),
    "partial_valid_violation": ModelStimulus(
        boxes=(OUTSIDE, INSIDE),
        subjects=((OUTSIDE, PARKED), (INSIDE, (UNPARKED[0], UNPARKED[2]))),
        image_variant="two",
    ),
}


@dataclass(frozen=True, slots=True)
class Frame:
    content: bytes = field(repr=False)
    digest: str


def read_frame(scene: Path, variant: str) -> Frame:
    if variant == "base":
        content = (scene / "assets" / "synthetic-frame.png").read_bytes()
        declared = BASE_FRAME_DIGEST
    elif variant in {"two", "wheel-on-tactile"}:
        name = "two-vehicles" if variant == "two" else variant
        content = (FIXTURES / f"{name}.png").read_bytes()
        declared = (FIXTURES / f"{name}.sha256").read_text().strip()
    else:
        raise ValueError(f"unknown fixture frame {variant}")
    observed = sha256(content).hexdigest()
    if observed != declared:
        raise ValueError("fixture frame no longer matches its immutable digest")
    return Frame(content, observed)
