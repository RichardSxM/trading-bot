"""covers: spec 0004 AC-15, root DESIGN.md and tokens.css hold the same tokens, and every listed pair passes AA."""

from pathlib import Path

import pytest
import yaml

from dashboard.ui.tokens import contrast_ratio, parse_tokens_css, read_tokens

# typography.<role>.<prop> → --type-<role>-<suffix>
TYPE_PROPS = {
    "fontFamily": "family",
    "fontSize": "size",
    "fontWeight": "weight",
    "lineHeight": "line",
    "letterSpacing": "tracking",
    "fontFeature": "feature",
}
TOKEN_GROUPS = {"colors", "typography", "rounded", "spacing", "motion"}
MINIMUMS = {"text": 4.5, "large": 3.0, "ui": 3.0}
SURFACES = ("bg", "surface", "raised")
REQUIRED_PAIRS = (
    {(fg, bg) for fg in ("text", "muted", "profit", "loss", "warn", "info") for bg in SURFACES}
    | {("faint", "bg"), ("faint", "surface"), ("on-alarm", "stop"), ("on-alarm", "live"), ("bg", "demo")}
    | {("edge", "bg")}
)


def design_md() -> Path:
    """Root DESIGN.md: /app/DESIGN.md in the image, the repo root in the tools container."""
    for folder in Path(__file__).resolve().parents:
        if (folder / "DESIGN.md").is_file():
            return folder / "DESIGN.md"
    raise FileNotFoundError("DESIGN.md not found above the tests")


def frontmatter(text):
    _, front, _ = text.split("---\n", 2)
    return yaml.safe_load(front)


def normalize(value):
    text = " ".join(str(value).split())
    return text.lower() if text.startswith("#") else text


def expected_css(front):
    """The custom properties DESIGN.md's tokens map to, by the naming rule in tokens.css."""
    names: dict[str, object] = dict(front["colors"])
    names |= {f"space-{key}": value for key, value in front["spacing"].items()}
    names |= {f"radius-{key}": value for key, value in front["rounded"].items()}
    names |= {f"motion-{key}": value for key, value in front["motion"].items()}
    for role, props in front["typography"].items():
        names |= {f"type-{role}-{TYPE_PROPS[prop]}": value for prop, value in props.items()}
    return {name: normalize(value) for name, value in names.items()}


@pytest.fixture(scope="module")
def front():
    return frontmatter(design_md().read_text(encoding="utf-8"))


def test_tokens_css_holds_exactly_the_design_md_tokens(front):
    css = {name: normalize(value) for name, value in read_tokens().items()}
    expected = expected_css(front)
    assert sorted(set(css) - set(expected)) == [], "in tokens.css but not in DESIGN.md"
    assert sorted(set(expected) - set(css)) == [], "in DESIGN.md but not in tokens.css"
    assert {n: v for n, v in css.items() if expected[n] != v} == {}


def test_the_frontmatter_has_only_known_groups_and_its_references_resolve(front):
    assert set(front) - {"name", "description", "components", "contrast_pairs"} == TOKEN_GROUPS
    for component, props in front["components"].items():
        for prop, value in props.items():
            for ref in [part.strip("{}") for part in str(value).split() if part.startswith("{")]:
                group, key = ref.split(".", 1)
                assert key in front[group], f"{component}.{prop} refers to missing {ref}"


def test_every_listed_pair_passes_wcag_aa(front):
    colors = {name: normalize(value) for name, value in front["colors"].items()}
    failing = {
        (pair["fg"], pair["bg"]): round(contrast_ratio(colors[pair["fg"]], colors[pair["bg"]]), 2)
        for pair in front["contrast_pairs"]
        if contrast_ratio(colors[pair["fg"]], colors[pair["bg"]]) < MINIMUMS[pair["use"]]
    }
    assert failing == {}


def test_the_pairs_the_spec_requires_are_listed(front):
    listed = {(pair["fg"], pair["bg"]) for pair in front["contrast_pairs"]}
    assert REQUIRED_PAIRS - listed == set()


def test_a_changed_value_or_a_weak_pair_is_caught(front):
    css = parse_tokens_css(":root { --bg: #0b0d11; }")
    assert css["bg"] != expected_css(front)["bg"]
    assert contrast_ratio("#232831", "#0b0d10") < MINIMUMS["ui"]  # rule on bg: never a control's only edge


def test_contrast_ratio_matches_the_wcag_extremes():
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21)
    assert contrast_ratio("#777777", "#777777") == pytest.approx(1)
    with pytest.raises(ValueError, match="not a #rrggbb color"):
        contrast_ratio("red", "#ffffff")
