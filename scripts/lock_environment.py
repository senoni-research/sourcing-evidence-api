"""Record the already-installed, tested dependency closure (not a universal resolver)."""

import tomllib
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement

root = Path(__file__).resolve().parents[1]
project = tomllib.loads((root / "pyproject.toml").read_text())["project"]


def closure(requirements):
    pinned = {}
    todo = list(requirements)
    while todo:
        requirement = Requirement(todo.pop())
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        dist = distribution(requirement.name)
        name = dist.metadata["Name"]
        if name.lower() in pinned:
            continue
        pinned[name.lower()] = f"{name}=={dist.version}"
        todo.extend(dist.requires or [])
    return (
        "# Exact versions exercised in this prototype; Python 3.12.\n"
        + "\n".join(pinned[name] for name in sorted(pinned))
        + "\n"
    )


(root / "requirements.lock").write_text(closure(project["dependencies"]))
(root / "requirements-dev.lock").write_text(
    closure(project["dependencies"] + project["optional-dependencies"]["dev"])
)
