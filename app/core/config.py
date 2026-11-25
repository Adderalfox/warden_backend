"""
Central settings for Warden. All infra endpoints/credentials are optional at
import time -- MCP servers degrade to clearly-labeled dry-run behavior when a
given backend isn't configured, but the *logic path* (k8s API calls, git
operations, GitHub API calls, ES queries) is always the real implementation,
never a hardcoded fixture.
"""
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WARDEN_", extra="ignore")

    # --- Core service ---
    environment: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Security ---
    webhook_signing_secret: str = "change-me-dev-secret"
    approval_api_key: str = "change-me-dev-key"

    # --- LLM ---
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-2.0-flash"

    # --- Kubernetes ---
    kubeconfig_path: Optional[str] = None  # None => in-cluster or default context
    k8s_namespace_default: str = "default"

    # --- Logs backend (Elasticsearch) ---
    elasticsearch_url: Optional[str] = None
    elasticsearch_api_key: Optional[str] = None
    elasticsearch_index: str = "app-logs-*"

    # --- Git / GitHub ---
    github_token: Optional[str] = None
    git_workdir: str = "/tmp/warden-repos"

    # --- Pod analytics (Grafana-style metrics) ---
    # kubernetes: real cluster (Kubernetes API + metrics-server). demo: synthetic data,
    # clearly labelled in the API/UI, for exploring the dashboard without a cluster.
    metrics_source: str = "kubernetes"          # kubernetes | demo
    metrics_enabled: bool = True                # run the background collector
    metrics_scrape_interval_seconds: int = 15
    metrics_retention_minutes: int = 360        # in-memory history kept by the collector
    metrics_namespaces: str = ""                # "" => default namespace; "*" => all; or "a,b"
    prometheus_url: Optional[str] = None        # optional: long history + network + throttling

    # --- MCP transport ---
    mcp_transport: str = "stdio"  # stdio | sse
    mcp_k8s_url: Optional[str] = None
    mcp_logs_url: Optional[str] = None
    mcp_git_url: Optional[str] = None


settings = Settings()
