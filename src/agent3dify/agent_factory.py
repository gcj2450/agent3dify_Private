from __future__ import annotations

import os
from typing import Callable, Any

from deepagents import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver

from .config import AgentModels
from .prompts import BUILDER_SYSTEM_PROMPT, SUPERVISOR_PROMPT, VERIFIER_SYSTEM_PROMPT
from .tools import make_compare_projection_pair_tool, make_image_editor_tool, make_crop_reference_view_tool, make_preprocess_reference_image_tool
from .workspace import Workspace
from .shell_backend import AgentShellBackend


def build_agent(
    workspace: Workspace, *, models: AgentModels, debug: bool = True,
    model_factory: Callable[[str], Any] | None = None,
    extra_instructions: str = "",
    backend: AgentShellBackend | None = None,
):
    backend = backend or AgentShellBackend(str(workspace.root))

    image_editor = None
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        image_editor = make_image_editor_tool(
            workspace,
            model_name=models.image_editor_model(),
            view_detector_model=models.view_detector_model(),
        )
    compare_projection_pair = make_compare_projection_pair_tool(workspace)
    resolve_model = model_factory or (lambda name: name)
    image_tools = [make_crop_reference_view_tool(workspace), make_preprocess_reference_image_tool(workspace)]
    if image_editor is not None:
        image_tools.append(image_editor)
    availability = "\nThe image_editor tool is unavailable. Inspect the original image directly; use crop_reference_view for deterministic crops if needed. Do not request Gemini image editing.\n" if image_editor is None else ""

    subagents = [
        {
            "name": "cadquery-builder",
            "description": "Build the CadQuery model from the reference drawing, optionally using image_editor outputs and verifier guidance, then export artifacts.",
            "model": resolve_model(models.builder_model()),
            "system_prompt": BUILDER_SYSTEM_PROMPT + extra_instructions,
            "tools": [],
            "skills": ["skills/builder"],
        },
        {
            "name": "render-verifier",
            "description": "Compare rendered projections against reference views, optionally using image_editor to isolate reference views, then write a concrete fix plan.",
            "model": resolve_model(models.verifier_model()),
            "system_prompt": VERIFIER_SYSTEM_PROMPT + availability + extra_instructions,
            "tools": [*image_tools, compare_projection_pair],
            "skills": ["skills/verifier"],
        },
    ]

    return create_deep_agent(
        name="drawing-to-cad-supervisor-local-shell",
        model=resolve_model(models.supervisor),
        backend=backend,
        checkpointer=InMemorySaver(),
        system_prompt=SUPERVISOR_PROMPT + availability + extra_instructions,
        tools=image_tools,
        subagents=subagents,
        debug=debug,
        # Strongly consider enabling approvals in real use:
        # interrupt_on={"execute": True},
    )
