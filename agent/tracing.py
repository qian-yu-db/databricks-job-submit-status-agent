import logging
import os

logger = logging.getLogger(__name__)


def enable_tracing() -> None:
    """Turn on MLflow tracing for the LangChain/LangGraph agent.

    When MLFLOW_EXPERIMENT is set (the deployed app), traces are logged to that
    workspace experiment via the Databricks tracking store; otherwise autolog
    falls back to MLflow's local default (dev). Non-fatal: a tracing/setup
    failure must never break a user request.
    """
    try:
        import mlflow

        experiment = os.environ.get("MLFLOW_EXPERIMENT")
        if experiment:
            mlflow.set_tracking_uri("databricks")
            mlflow.set_experiment(experiment)
        mlflow.langchain.autolog()
    except Exception as exc:  # pragma: no cover
        logger.warning("MLflow tracing disabled (%s)", exc)
