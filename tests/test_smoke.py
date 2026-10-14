"""
Smoke test: verify the three core libraries import without error.
Does NOT test agent behaviour — that comes later.
Run with: pytest tests/test_smoke.py -v
"""
import importlib


def test_pydantic_imports() -> None:
    pydantic = importlib.import_module("pydantic")
    assert hasattr(pydantic, "BaseModel")
    # Confirm we are in the compatible range: >=2.12, <2.13
    from pydantic.version import VERSION
    major, minor, *_ = VERSION.split(".")
    assert (int(major), int(minor)) == (2, 12), (
        f"pydantic {VERSION} is outside the safe window (==2.12.x). "
        "crewai requires pydantic<2.13. Re-check requirements.txt."
    )


def test_google_adk_imports() -> None:
    adk = importlib.import_module("google.adk")
    assert hasattr(adk, "__version__")
    from google.adk.agents import LlmAgent  # noqa: F401


def test_crewai_imports() -> None:
    crewai = importlib.import_module("crewai")
    assert hasattr(crewai, "Agent")
    assert hasattr(crewai, "Crew")
    assert hasattr(crewai, "Task")
    # Confirm native A2A module is present (resolves OPEN item 5)
    from crewai import a2a  # noqa: F401
    assert hasattr(a2a, "A2AServerConfig")
    assert hasattr(a2a, "A2AClientConfig")
