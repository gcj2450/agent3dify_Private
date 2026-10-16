from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from .app import run
from .config import AgentModels


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drawing", help="Path to the source drawing image")
    parser.add_argument("--web", action="store_true", help="Start the interactive CAD web workbench")
    parser.add_argument("--host", default="127.0.0.1", help="Web interface bind address")
    parser.add_argument("--port", type=int, default=7860, help="Web interface port")
    parser.add_argument(
        "--model",
        "--supervisor-model",
        dest="supervisor_model",
        help="Model for the supervisor agent. Overrides SUPERVISOR_MODEL or AGENT_MODEL.",
    )
    parser.add_argument(
        "--image-editor-model",
        help="Model for the image_editor tool. Overrides IMAGE_EDITOR_MODEL.",
    )
    parser.add_argument(
        "--view-detector-model",
        help="Model for extract_view detection. Overrides VIEW_DETECTOR_MODEL.",
    )
    parser.add_argument(
        "--builder-model",
        "--modeler-model",
        dest="builder_model",
        help="Model for the cadquery-builder subagent. Defaults to the supervisor model.",
    )
    parser.add_argument(
        "--verifier-model",
        help="Model for the render-verifier subagent. Defaults to the supervisor model.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv()
    if args.web:
        from .web import serve
        return serve(args.host, args.port)
    if not args.drawing:
        parser.error("provide --drawing or use --web to start the interface")
    models = AgentModels.from_env().with_overrides(
        supervisor=args.supervisor_model,
        image_editor=args.image_editor_model,
        view_detector=args.view_detector_model,
        builder=args.builder_model,
        verifier=args.verifier_model,
    )
    return run(Path(args.drawing), models=models)
