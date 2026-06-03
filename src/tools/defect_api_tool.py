"""
Defect Prediction API Tool
===========================
Calls the Defect Prediction Engine (from Project 1) to get
risk scores and SHAP explanations for files in a repository.

The agent uses this to prioritise which files to review first
and to understand WHY the ML model flagged a file.
"""

import requests
from loguru import logger
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import DEFECT_API
from src.tools.registry import Tool, ToolRegistry


def get_repo_risk_scores(repo_url: str, top_k: int = 20) -> str:
    """Call the Defect Prediction API to get risk scores for all files in a repository.

    Returns a ranked list of risky files with their scores, labels, and top SHAP
    feature.  Falls back gracefully if the API is not running — the agent continues
    the review without ML context rather than failing.

    Args:
        repo_url: GitHub URL of the repository to analyse.
        top_k: Number of top risky files to return (default 20).

    Returns:
        Formatted string with ranked risk scores, or a warning message if the API
        is unavailable.
    """
    base_url = DEFECT_API["base_url"]

    try:
        logger.info(f"Calling Defect Prediction API for {repo_url}")
        response = requests.post(
            f"{base_url}/analyze",
            json={
                "repo_url": repo_url,
                "top_k": top_k,
                "use_hybrid": DEFECT_API["use_hybrid"],
            },
            timeout=DEFECT_API["timeout_seconds"],
        )
        response.raise_for_status()
        data = response.json()

        results = data.get("top_k_results", [])
        if not results:
            return "Defect API returned no results — no high-risk files identified."

        lines = [
            f"Defect Prediction API results for {repo_url}:",
            f"Model: {data.get('model_used', 'unknown')} | "
            f"AUC: {data.get('model_auc', 0):.3f} | "
            f"Analysis time: {data.get('analysis_time_ms', 0):.0f}ms\n",
            "Risk-ranked files:",
        ]

        for r in results:
            score = r.get("risk_score", 0)
            label = r.get("risk_label", "?")
            file_path = r.get("file_path", "?")
            top_feature = ""
            if r.get("top_shap_features"):
                f = r["top_shap_features"][0]
                top_feature = (
                    f" | top risk factor: {f['feature_name']}={f['feature_value']}"
                )

            lines.append(f"  [{label}] {score:.3f} — {file_path}{top_feature}")

        return "\n".join(lines)

    except requests.exceptions.ConnectionError:
        return (
            "WARNING: Defect Prediction API not reachable at "
            f"{base_url}. Proceeding without ML risk scores. "
            "Start the API with: uvicorn src.api.main:app --port 8000"
        )
    except requests.exceptions.Timeout:
        return (
            f"WARNING: Defect Prediction API timed out after "
            f"{DEFECT_API['timeout_seconds']}s."
        )
    except Exception as e:
        return f"WARNING: Defect API error: {e}. Proceeding without ML context."


def get_file_explanation(repo_url: str, job_id: str, file_path: str) -> str:
    """Get a detailed SHAP explanation for a specific file from the Defect Prediction API.

    Shows exactly why the ML model flagged the file as risky by presenting a
    SHAP waterfall of the top contributing features.

    Args:
        repo_url: Repository URL — used to identify the analysis context.
        job_id: Job ID string returned by a previous get_repo_risk_scores call.
        file_path: The specific file path to explain.

    Returns:
        Formatted SHAP explanation string, or an error message if unavailable.
    """
    base_url = DEFECT_API["base_url"]

    try:
        response = requests.get(
            f"{base_url}/explain/{job_id}/{file_path}",
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        lines = [
            f"ML Explanation for {file_path}:",
            f"Risk Score: {data.get('risk_score', 0):.3f} ({data.get('risk_label', '?')})",
            f"\nPlain English: {data.get('plain_english_summary', 'N/A')}",
            "\nTop contributing features (SHAP waterfall):",
        ]

        for feature in data.get("shap_waterfall", [])[:8]:
            direction = (
                "↑ increases risk" if feature["shap_value"] > 0 else "↓ decreases risk"
            )
            lines.append(
                f"  {feature['feature_name']}: {feature['feature_value']} "
                f"(SHAP: {feature['shap_value']:+.3f}) {direction}"
            )

        return "\n".join(lines)

    except Exception as e:
        return f"Could not get ML explanation: {e}"


def register_defect_api_tools(registry: ToolRegistry) -> None:
    """Register Defect Prediction API tools into the given registry.

    Args:
        registry: The ToolRegistry instance to register tools into.
    """
    registry.register(Tool(
        name="get_repo_risk_scores",
        description=(
            "Call the ML Defect Prediction API to get risk scores for all files. "
            "Use this at the START of reviewing a repository to prioritise which "
            "files need attention most."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo_url": {"type": "string", "description": "GitHub URL of the repository"},
                "top_k": {
                    "type": "integer",
                    "description": "Number of risky files to return (default 20)",
                },
            },
            "required": ["repo_url"],
        },
        func=get_repo_risk_scores,
        category="defect_api",
    ))

    registry.register(Tool(
        name="get_file_explanation",
        description=(
            "Get detailed SHAP explanation from ML model for why a specific file "
            "was flagged as risky."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo_url": {"type": "string", "description": "Repository URL"},
                "job_id": {
                    "type": "string",
                    "description": "Job ID from get_repo_risk_scores",
                },
                "file_path": {"type": "string", "description": "File path to explain"},
            },
            "required": ["repo_url", "job_id", "file_path"],
        },
        func=get_file_explanation,
        category="defect_api",
    ))