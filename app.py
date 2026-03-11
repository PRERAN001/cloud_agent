from __future__ import annotations

import json
import os
import random
import re
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

BASE_DIR = Path("deployments")
BASE_DIR.mkdir(parents=True, exist_ok=True)

LOCK = threading.Lock()
SESSIONS: dict[str, dict[str, Any]] = {}
REVIEW_CACHE: dict[str, dict[str, Any]] = {}


@app.route("/", methods=["GET"])
def index() -> Any:
    return render_template("index.html")


@dataclass
class CommandResult:
    ok: bool
    code: int
    stdout: str
    stderr: str
    command: str
    cwd: str


def run_command(command: str, cwd: Path, timeout: int = 900) -> CommandResult:
    proc = subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(
        ok=proc.returncode == 0,
        code=proc.returncode,
        stdout=(proc.stdout or "").strip(),
        stderr=(proc.stderr or "").strip(),
        command=command,
        cwd=str(cwd),
    )


def validate_github_repo_url(repo_url: str) -> tuple[bool, str]:
    parsed = urlparse(repo_url)
    if parsed.scheme not in {"http", "https", "git", "ssh"}:
        return False, "Unsupported repository URL scheme"

    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return False, "Only GitHub repositories are supported"

    # Basic SSH GitHub support: git@github.com:owner/repo.git
    if parsed.scheme == "ssh" and "github.com" not in parsed.netloc.lower():
        return False, "Only GitHub repositories are supported"

    if "github.com" not in repo_url.lower():
        return False, "Repository URL must point to GitHub"

    return True, "ok"


def repo_name_from_url(repo_url: str) -> str:
    cleaned = repo_url.rstrip("/").split("/")[-1]
    cleaned = cleaned.replace(".git", "")
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "-", cleaned)
    cleaned = cleaned.strip(".-_").lower()

    # Avoid Windows reserved folder names.
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
    }

    if not cleaned or cleaned in reserved:
        cleaned = f"repo-{int(time.time())}"
    return cleaned


def docker_safe_name(name: str) -> str:
    # Docker image/container safe slug.
    slug = (name or "").lower().strip()
    slug = re.sub(r"[^a-z0-9_.-]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-._")
    if not slug:
        slug = f"repo-{int(time.time())}"
    return slug


def detect_project_type(repo_path: Path) -> str:
    if (repo_path / "requirements.txt").exists() or any(repo_path.glob("**/*.py")):
        return "python"
    if (repo_path / "package.json").exists():
        return "node"
    return "unknown"


def detect_framework_details(repo_path: Path) -> dict[str, Any]:
    """Agent 4: Detect exact framework and subtype (React/Vite/Next.js, Express/Fastify/NestJS, Flask/FastAPI/Django/ML)."""
    base_type = detect_project_type(repo_path)
    result: dict[str, Any] = {
        "base_type": base_type,
        "framework": "unknown",
        "subtype": None,
        "is_fullstack": False,
        "build_tool": None,
    }

    if base_type == "node":
        pkg_json_path = repo_path / "package.json"
        if pkg_json_path.exists():
            try:
                data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
                all_deps: dict[str, str] = {
                    **data.get("dependencies", {}),
                    **data.get("devDependencies", {}),
                }
                if "react" in all_deps:
                    result["framework"] = "react"
                    if "next" in all_deps:
                        result["subtype"] = "nextjs"
                        result["build_tool"] = "next"
                    elif "vite" in all_deps or any("@vitejs" in k for k in all_deps):
                        result["subtype"] = "vite"
                        result["build_tool"] = "vite"
                    else:
                        result["subtype"] = "cra"
                        result["build_tool"] = "react-scripts"
                elif "@nestjs/core" in all_deps:
                    result["framework"] = "nestjs"
                elif "fastify" in all_deps:
                    result["framework"] = "fastify"
                elif "express" in all_deps:
                    result["framework"] = "express"
                else:
                    result["framework"] = "node"

                if not result["build_tool"]:
                    scripts = data.get("scripts", {})
                    build_script = scripts.get("build", "") if isinstance(scripts, dict) else ""
                    if "webpack" in build_script:
                        result["build_tool"] = "webpack"
                    elif "vite" in build_script:
                        result["build_tool"] = "vite"
            except Exception:
                pass

    elif base_type == "python":
        req_text = ""
        req_file = repo_path / "requirements.txt"
        if req_file.exists():
            try:
                req_text = req_file.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                pass

        import_text = ""
        for src_file in list(repo_path.glob("*.py"))[:10]:
            try:
                import_text += src_file.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                pass

        combined = req_text + import_text
        if "fastapi" in combined:
            result["framework"] = "fastapi"
            result["build_tool"] = "uvicorn"
        elif "django" in combined:
            result["framework"] = "django"
            result["build_tool"] = "django"
        elif "flask" in combined:
            result["framework"] = "flask"
            result["build_tool"] = "flask"
        elif is_streamlit_project(repo_path):
            result["framework"] = "streamlit"
            result["build_tool"] = "streamlit"
        elif any(ml in combined for ml in ["tensorflow", "torch", "scikit-learn", "sklearn", "keras", "pandas", "numpy"]):
            result["framework"] = "ml"
        else:
            result["framework"] = "python"

        if (repo_path / "package.json").exists():
            result["is_fullstack"] = True

    return result


def is_streamlit_project(repo_path: Path) -> bool:
    requirements = repo_path / "requirements.txt"
    if requirements.exists():
        req_text = requirements.read_text(encoding="utf-8", errors="ignore").lower()
        if "streamlit" in req_text:
            return True

    for source in repo_path.glob("**/*.py"):
        content = source.read_text(encoding="utf-8", errors="ignore").lower()
        if "import streamlit" in content or "from streamlit" in content:
            return True
    return False


def find_streamlit_entrypoint(repo_path: Path) -> str:
    preferred = ["app.py", "main.py"]
    for candidate in preferred:
        target = repo_path / candidate
        if target.exists():
            content = target.read_text(encoding="utf-8", errors="ignore").lower()
            if "import streamlit" in content or "from streamlit" in content:
                return candidate

    for source in repo_path.glob("*.py"):
        content = source.read_text(encoding="utf-8", errors="ignore").lower()
        if "import streamlit" in content or "from streamlit" in content:
            return source.name
    return find_python_entrypoint(repo_path)


def detect_env_keys(repo_path: Path) -> list[str]:
    patterns = [
        re.compile(r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]\s*\)"),
        re.compile(r"os\.environ\[\s*['\"]([A-Z0-9_]+)['\"]\s*\]"),
        re.compile(r"process\.env\.([A-Z0-9_]+)"),
    ]
    keys: set[str] = set()

    for source in list(repo_path.glob("**/*.py")) + list(repo_path.glob("**/*.js")):
        try:
            content = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in patterns:
            for match in pattern.findall(content):
                if match and len(match) > 2:
                    keys.add(match)

    return sorted(keys)


def write_env_file(repo_path: Path, env_values: dict[str, str], required_keys: list[str]) -> Path:
    env_path = repo_path / ".env"
    if env_path.exists():
        return env_path

    lines: list[str] = []
    for key in required_keys:
        value = env_values.get(key, f"CHANGE_ME_{key}")
        lines.append(f"{key}={value}")

    if not lines:
        lines.append("# Add required environment variables here")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


def write_env_example(repo_path: Path, env_keys: list[str]) -> Path:
    """Agent 3: Generate .env.example documenting required environment variables."""
    env_example_path = repo_path / ".env.example"
    lines = ["# Copy this file to .env and fill in values", ""]
    if env_keys:
        for key in env_keys:
            lines.append(f"{key}=")
    else:
        lines.append("# No environment variables detected")
    env_example_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_example_path


def ensure_gitignore_has_env(repo_path: Path) -> bool:
    """Agent 3: Ensure .env is listed in .gitignore. Returns True if the file was modified."""
    gitignore_path = repo_path / ".gitignore"
    if gitignore_path.exists():
        try:
            content = gitignore_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            content = ""
        lines = content.splitlines()
        if ".env" in lines or "*.env" in lines:
            return False
        with open(gitignore_path, "a", encoding="utf-8") as f:
            f.write("\n.env\n")
        return True
    gitignore_path.write_text(".env\n", encoding="utf-8")
    return True


def validate_dependencies(repo_path: Path, project_type: str) -> dict[str, Any]:
    """Agent 2: Validate dependency files and report missing or problematic configurations."""
    found_files: list[str] = []
    missing_files: list[str] = []
    issues: list[dict[str, str]] = []

    if project_type == "python":
        dep_files = ["requirements.txt", "pyproject.toml", "Pipfile"]
        for f in dep_files:
            if (repo_path / f).exists():
                found_files.append(f)
        if not found_files:
            missing_files.append("requirements.txt")
            issues.append({
                "severity": "high",
                "message": "No Python dependency file found. Create requirements.txt, pyproject.toml, or Pipfile.",
            })
        if not (repo_path / "requirements.lock").exists() and not (repo_path / "poetry.lock").exists() and "requirements.txt" in found_files:
            issues.append({
                "severity": "low",
                "message": "Consider using pip-compile or Poetry to generate a lock file for reproducible builds.",
            })

    elif project_type == "node":
        if not (repo_path / "package.json").exists():
            missing_files.append("package.json")
            issues.append({"severity": "high", "message": "package.json not found."})
        else:
            found_files.append("package.json")
            lock_files = ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"]
            found_locks = [f for f in lock_files if (repo_path / f).exists()]
            if found_locks:
                found_files.extend(found_locks)
            else:
                missing_files.extend(lock_files)
                issues.append({
                    "severity": "medium",
                    "message": "No lock file found (package-lock.json/yarn.lock/pnpm-lock.yaml). Commit a lock file for reproducible installs.",
                })

    passed = not any(i["severity"] == "high" for i in issues)
    return {
        "found_files": found_files,
        "missing_files": missing_files,
        "issues": issues,
        "passed": passed,
    }


def validate_build(repo_path: Path, project_type: str, framework: str) -> dict[str, Any]:
    """Agent 5: Validate the project build and generate run/build commands."""
    steps: list[dict[str, Any]] = []
    build_scripts: dict[str, str] = {}

    if project_type == "python":
        req_file = repo_path / "requirements.txt"
        if req_file.exists():
            try:
                req_lines = req_file.read_text(encoding="utf-8").splitlines()
                valid = [ln for ln in req_lines if ln.strip() and not ln.strip().startswith("#")]
                steps.append({"step": "requirements.txt", "ok": True, "details": f"{len(valid)} packages listed"})
            except OSError as exc:
                steps.append({"step": "requirements.txt", "ok": False, "details": str(exc)})
        else:
            steps.append({"step": "requirements.txt", "ok": False, "details": "File not found"})

        syntax = run_command("python -m compileall -q .", repo_path, timeout=120)
        steps.append({
            "step": "python syntax check",
            "ok": syntax.ok,
            "details": (syntax.stderr or syntax.stdout or "ok")[:500],
        })

        entry = find_python_entrypoint(repo_path)
        module = entry.replace(".py", "").replace("/", ".")
        if framework == "fastapi":
            build_scripts["start"] = f"uvicorn {module}:app --host 0.0.0.0 --port 5000"
        elif framework == "flask":
            build_scripts["start"] = "flask run --host=0.0.0.0 --port=5000"
        elif framework == "django":
            build_scripts["start"] = "python manage.py runserver 0.0.0.0:5000"
        elif framework == "streamlit":
            build_scripts["start"] = f"streamlit run {entry} --server.port 5000 --server.address 0.0.0.0"
        else:
            build_scripts["start"] = f"python {entry}"

    elif project_type == "node":
        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                scripts = data.get("scripts", {}) if isinstance(data.get("scripts"), dict) else {}
                if "build" in scripts:
                    build_scripts["build"] = scripts["build"]
                    steps.append({"step": "build script", "ok": True, "details": scripts["build"]})
                else:
                    steps.append({"step": "build script", "ok": False, "details": "No 'build' script in package.json"})
                if "start" in scripts:
                    build_scripts["start"] = scripts["start"]
                    steps.append({"step": "start script", "ok": True, "details": scripts["start"]})
                else:
                    steps.append({"step": "start script", "ok": False, "details": "No 'start' script in package.json"})
                steps.append({"step": "package.json valid", "ok": True, "details": "Parsed successfully"})
            except Exception as exc:
                steps.append({"step": "package.json parse", "ok": False, "details": str(exc)})
        else:
            steps.append({"step": "package.json", "ok": False, "details": "File not found"})

    success = len(steps) > 0 and all(s["ok"] for s in steps)
    return {"steps": steps, "success": success, "build_scripts": build_scripts}


def generate_readme_content(
    repo_path: Path,
    project_type: str,
    framework: str,
    env_keys: list[str],
    build_info: dict[str, Any],
) -> str:
    """Agent 9: Generate a professional README.md for the analyzed repository."""
    repo_name = repo_path.name.replace("-", " ").replace("_", " ").title()

    lang_map = {"python": "Python", "node": "Node.js"}
    framework_map = {
        "flask": "Flask",
        "fastapi": "FastAPI",
        "django": "Django",
        "express": "Express",
        "fastify": "Fastify",
        "nestjs": "NestJS",
        "react": "React",
        "streamlit": "Streamlit",
        "ml": "Python (Machine Learning)",
    }
    lang = lang_map.get(project_type, project_type.title())
    fw = framework_map.get(framework, framework.title())

    install_cmd = "pip install -r requirements.txt" if project_type == "python" else "npm install"

    start_cmd = build_info.get("build_scripts", {}).get("start", "")
    if not start_cmd:
        start_cmd = f"python {find_python_entrypoint(repo_path)}" if project_type == "python" else "npm start"

    build_cmd = build_info.get("build_scripts", {}).get("build", "")
    build_section = f"\n```bash\n# Build\n{build_cmd}\n```\n" if build_cmd else ""

    env_section = ""
    if env_keys:
        rows = "\n".join(f"| `{k}` | *(required)* |" for k in env_keys)
        env_section = (
            "\n## Environment Setup\n\n"
            "Copy `.env.example` to `.env`:\n\n"
            "```bash\ncp .env.example .env\n```\n\n"
            "Required environment variables:\n\n"
            "| Variable | Description |\n"
            "|----------|-------------|\n"
            f"{rows}\n"
        )

    slug = repo_path.name
    docker_section = (
        "## Docker Usage\n\n"
        "```bash\n"
        f"# Build the image\ndocker build -t {slug} .\n\n"
        f"# Run the container\ndocker run -p 5000:5000 {slug}\n"
        "```\n"
    )

    api_section = ""
    if project_type in ("python", "node") and framework not in ("react", "ml", "streamlit"):
        api_section = (
            "\n## API Usage\n\n"
            "After starting the server the API is available at `http://localhost:5000`.\n"
            "Refer to the source code for available endpoints.\n"
        )

    return (
        f"# {repo_name}\n\n"
        f"## Overview\n\nA {fw} application built with {lang}.\n\n"
        f"## Tech Stack\n\n- **Language**: {lang}\n- **Framework**: {fw}\n\n"
        f"## Installation\n\n```bash\n{install_cmd}\n```\n\n"
        f"## Running the Application\n\n```bash\n{start_cmd}\n```\n"
        f"{build_section}"
        f"{env_section}"
        f"{docker_section}"
        f"{api_section}"
        "\n## Contributing\n\n"
        "1. Fork the repository\n"
        "2. Create a feature branch (`git checkout -b feature/improvement`)\n"
        "3. Commit your changes\n"
        "4. Push and open a Pull Request\n"
        "\n## License\n\nSee [LICENSE](LICENSE) for details.\n"
    )


def find_python_entrypoint(repo_path: Path) -> str:
    preferred = ["app.py", "main.py", "run.py", "wsgi.py"]
    for candidate in preferred:
        if (repo_path / candidate).exists():
            return candidate

    for file in repo_path.glob("*.py"):
        return file.name

    return "app.py"


def get_node_start_command(repo_path: Path) -> str:
    package_json = repo_path / "package.json"
    if not package_json.exists():
        return "node index.js"
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except Exception:
        return "npm start"

    scripts = data.get("scripts", {})
    if isinstance(scripts, dict) and "start" in scripts:
        return "npm start"
    main = data.get("main")
    if isinstance(main, str) and main.strip():
        return f"node {main}"
    return "node index.js"


def extract_exposed_port(dockerfile_text: str, default_port: int) -> int:
    match = re.search(r"^\s*EXPOSE\s+(\d+)", dockerfile_text, flags=re.IGNORECASE | re.MULTILINE)
    if match:
        return int(match.group(1))
    return default_port


def generate_dockerfile(repo_path: Path, project_type: str) -> tuple[Path, bool, int, str]:
    dockerfile_path = repo_path / "Dockerfile"
    existed = dockerfile_path.exists()
    default_port = 5000 if project_type == "python" else 3000
    streamlit_project = project_type == "python" and is_streamlit_project(repo_path)

    if existed:
        content = dockerfile_path.read_text(encoding="utf-8", errors="ignore")
        container_port = extract_exposed_port(content, default_port)
        fixed = False

        if "EXPOSE" not in content.upper():
            content += f"\nEXPOSE {container_port}\n"
            fixed = True

        if project_type == "python":
            if streamlit_project:
                streamlit_entry = find_streamlit_entrypoint(repo_path)
                desired_cmd = (
                    f"CMD [\"streamlit\", \"run\", \"{streamlit_entry}\", "
                    f"\"--server.port\", \"{container_port}\", \"--server.address\", \"0.0.0.0\"]"
                )
                if "streamlit" not in content.lower() or "CMD" not in content.upper():
                    content += f"{desired_cmd}\n"
                    fixed = True
            elif "CMD" not in content.upper():
                entry = find_python_entrypoint(repo_path)
                content += f"CMD [\"python\", \"{entry}\"]\n"
                fixed = True

        if project_type == "node" and "CMD" not in content.upper():
            cmd = get_node_start_command(repo_path)
            if cmd == "npm start":
                content += "CMD [\"npm\", \"start\"]\n"
            else:
                node_file = cmd.replace("node ", "", 1)
                content += f"CMD [\"node\", \"{node_file}\"]\n"
            fixed = True

        if fixed:
            dockerfile_path.write_text(content, encoding="utf-8")
            return dockerfile_path, True, container_port, "Dockerfile existed and was auto-corrected"
        return dockerfile_path, False, container_port, "Dockerfile existed and looked usable"

    if project_type == "python":
        entry = find_python_entrypoint(repo_path)
        if streamlit_project:
            entry = find_streamlit_entrypoint(repo_path)
            docker_content = (
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY requirements.txt /app/requirements.txt\n"
                "RUN pip install --no-cache-dir -r /app/requirements.txt\n"
                "COPY . /app\n"
                "EXPOSE 5000\n"
                f'CMD ["streamlit", "run", "{entry}", "--server.port", "5000", "--server.address", "0.0.0.0"]\n'
            )
        else:
            docker_content = (
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY requirements.txt /app/requirements.txt\n"
                "RUN pip install --no-cache-dir -r /app/requirements.txt\n"
                "COPY . /app\n"
                "EXPOSE 5000\n"
                f'CMD ["python", "{entry}"]\n'
            )
        container_port = 5000
    elif project_type == "node":
        framework_info = detect_framework_details(repo_path)
        fw = framework_info.get("framework", "node")
        subtype = framework_info.get("subtype") or ""
        if fw == "react":
            if subtype == "nextjs":
                docker_content = (
                    "FROM node:20-alpine\n"
                    "WORKDIR /app\n"
                    "COPY package*.json /app/\n"
                    "RUN npm install\n"
                    "COPY . /app\n"
                    "RUN npm run build\n"
                    "EXPOSE 3000\n"
                    'CMD ["npm", "start"]\n'
                )
                container_port = 3000
            else:
                # Vite or CRA — multi-stage build with Nginx
                dist_dir = "dist" if subtype == "vite" else "build"
                docker_content = (
                    "# Build stage\n"
                    "FROM node:20-alpine AS builder\n"
                    "WORKDIR /app\n"
                    "COPY package*.json /app/\n"
                    "RUN npm install\n"
                    "COPY . /app\n"
                    "RUN npm run build\n\n"
                    "# Production stage\n"
                    "FROM nginx:alpine\n"
                    f"COPY --from=builder /app/{dist_dir} /usr/share/nginx/html\n"
                    "EXPOSE 80\n"
                    'CMD ["nginx", "-g", "daemon off;"]\n'
                )
                container_port = 80
        else:
            docker_content = (
                "FROM node:20-alpine\n"
                "WORKDIR /app\n"
                "COPY package*.json /app/\n"
                "RUN npm install\n"
                "COPY . /app\n"
                "EXPOSE 3000\n"
                'CMD ["npm", "start"]\n'
            )
            container_port = 3000
    else:
        raise ValueError("Cannot generate Dockerfile for unknown project type")

    dockerfile_path.write_text(docker_content, encoding="utf-8")
    return dockerfile_path, True, container_port, "Dockerfile was generated"


def clone_or_update_repo(repo_url: str) -> tuple[Path, str, str]:
    repo_name = repo_name_from_url(repo_url)
    repo_path = BASE_DIR / repo_name

    if not repo_path.exists():
        result = run_command(
            f"git clone --depth 1 {shlex.quote(repo_url)} {shlex.quote(repo_name)}",
            BASE_DIR,
        )
        if not result.ok:
            raise RuntimeError(f"Clone failed: {result.stderr or result.stdout}")
        return repo_path, repo_name, result.stdout or "Repository cloned"

    if (repo_path / ".git").exists():
        fetch = run_command("git fetch --all --prune", repo_path)
        pull = run_command("git pull --ff-only", repo_path)
        details = (
            "\n".join(
                x
                for x in [fetch.stdout or fetch.stderr, pull.stdout or pull.stderr]
                if x
            ).strip()
            or "Repository already existed"
        )
        return repo_path, repo_name, details

    raise RuntimeError(f"Path exists but is not a git repository: {repo_path}")


def run_project_agents(repo_path: Path) -> dict[str, Any]:
    def cfg_agent() -> dict[str, Any]:
        files = {
            "requirements.txt": (repo_path / "requirements.txt").exists(),
            "package.json": (repo_path / "package.json").exists(),
            "Dockerfile": (repo_path / "Dockerfile").exists(),
            "README.md": (repo_path / "README.md").exists(),
        }
        return {"config_files": files}

    def type_agent() -> dict[str, Any]:
        return {"project_type": detect_project_type(repo_path)}

    def env_agent() -> dict[str, Any]:
        return {"required_env_keys": detect_env_keys(repo_path)}

    def docker_agent() -> dict[str, Any]:
        docker = repo_path / "Dockerfile"
        if not docker.exists():
            return {"dockerfile_status": "missing"}
        content = docker.read_text(encoding="utf-8", errors="ignore")
        return {
            "dockerfile_status": "present",
            "has_expose": "EXPOSE" in content.upper(),
            "has_cmd": "CMD" in content.upper(),
        }

    def framework_agent() -> dict[str, Any]:
        return {"framework_details": detect_framework_details(repo_path)}

    def dependency_agent() -> dict[str, Any]:
        project_type = detect_project_type(repo_path)
        return {"dependency_validation": validate_dependencies(repo_path, project_type)}

    jobs = {
        "config_agent": cfg_agent,
        "project_type_agent": type_agent,
        "env_agent": env_agent,
        "docker_agent": docker_agent,
        "framework_agent": framework_agent,
        "dependency_agent": dependency_agent,
    }

    output: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {executor.submit(func): name for name, func in jobs.items()}
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                output[name] = future.result()
            except Exception as exc:  # pragma: no cover
                output[name] = {"error": str(exc)}
    return output


def get_repo_head_sha(repo_path: Path) -> str:
    head = run_command("git rev-parse HEAD", repo_path, timeout=60)
    if head.ok and head.stdout:
        return head.stdout.strip()
    # fallback for non-git edge cases
    return f"nogit-{int(time.time())}"


def run_repo_review_checks(
    repo_path: Path,
    project_type: str,
    *,
    quick_mode: bool = False,
    max_findings: int = 200,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    def add_finding(
        severity: str,
        title: str,
        details: str,
        file_path: str | None = None,
    ) -> None:
        findings.append(
            {
                "severity": severity,
                "title": title,
                "details": details,
                "file": file_path,
            }
        )

    exclude_dirs = {
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        "dist",
        "build",
        ".next",
        "coverage",
    }
    scanned_files = 0
    todo_hits = 0
    large_files: list[tuple[str, int]] = []

    secret_patterns = [
        (re.compile(r"AKIA[0-9A-Z]{16}"), "Possible AWS access key"),
        (re.compile(r"ghp_[A-Za-z0-9]{36}"), "Possible GitHub personal access token"),
        (re.compile(r"AIza[0-9A-Za-z-_]{35}"), "Possible Google API key"),
        (
            re.compile(
                r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"
            ),
            "Possible hard-coded credential",
        ),
    ]
    benign_secret_markers = ("change_me", "your_", "example", "placeholder")

    for file in repo_path.rglob("*"):
        if not file.is_file():
            continue

        rel = file.relative_to(repo_path)
        if any(part in exclude_dirs for part in rel.parts):
            continue

        scanned_files += 1

        try:
            size = file.stat().st_size
        except OSError:
            continue

        if size > 2 * 1024 * 1024:
            large_files.append((str(rel), size))

        content_scan_limit = 256 * 1024 if quick_mode else 512 * 1024
        if size > content_scan_limit:
            # Skip deep content scan for larger files to keep checks responsive.
            continue

        try:
            content = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        lowered = content.lower()
        if "todo" in lowered or "fixme" in lowered:
            todo_hits += len(re.findall(r"(?i)\b(todo|fixme)\b", lowered))

        for pattern, label in secret_patterns:
            match = pattern.search(content)
            if not match:
                continue
            candidate = match.group(0).lower()
            if any(marker in candidate for marker in benign_secret_markers):
                continue
            add_finding(
                "high",
                label,
                "Potential secret found in source. Move credentials to environment variables.",
                str(rel),
            )
            if len(findings) >= max_findings:
                break

        if len(findings) >= max_findings:
            add_finding(
                "low",
                "Finding limit reached",
                f"Review truncated at {max_findings} findings for responsiveness.",
            )
            break

    if not (repo_path / "README.md").exists():
        add_finding("low", "README missing", "Repository should include setup and usage notes.")

    if not (repo_path / "LICENSE").exists() and not (repo_path / "LICENSE.md").exists():
        add_finding("low", "License file missing", "Consider adding LICENSE for clarity and compliance.")

    workflows = list((repo_path / ".github" / "workflows").glob("*.y*ml")) if (repo_path / ".github" / "workflows").exists() else []
    if not workflows:
        add_finding("medium", "CI workflow missing", "No GitHub Actions workflow detected in .github/workflows.")

    if project_type == "python":
        test_files = list(repo_path.glob("tests/**/*.py")) + list(repo_path.glob("**/test_*.py")) + list(repo_path.glob("**/*_test.py"))
        if not test_files:
            add_finding("medium", "Tests not detected", "No obvious Python test files found.")

        req_file = repo_path / "requirements.txt"
        if req_file.exists():
            try:
                req_lines = req_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                req_lines = []
            unpinned = []
            for line in req_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                    continue
                if any(op in stripped for op in ("==", ">=", "<=", "~=", "!=", ">", "<")):
                    continue
                unpinned.append(stripped)
            if unpinned:
                add_finding(
                    "medium",
                    "Unpinned Python dependencies",
                    f"Pin package versions in requirements.txt (examples: {', '.join(unpinned[:5])}).",
                    "requirements.txt",
                )

        syntax_check = run_command("python -m compileall -q .", repo_path, timeout=300)
        if not syntax_check.ok:
            add_finding(
                "high",
                "Python syntax/compile check failed",
                syntax_check.stderr or syntax_check.stdout or "compileall returned errors",
            )

    if project_type == "node":
        test_markers = [
            repo_path / "tests",
            repo_path / "__tests__",
            repo_path / "vitest.config.js",
            repo_path / "jest.config.js",
        ]
        if not any(marker.exists() for marker in test_markers):
            add_finding("medium", "Tests not detected", "No obvious Node test folder/config found.")

        package_json = repo_path / "package.json"
        if package_json.exists():
            try:
                package_data = json.loads(package_json.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                package_data = {}

            bad_versions: list[str] = []
            for section in ("dependencies", "devDependencies"):
                deps = package_data.get(section, {})
                if not isinstance(deps, dict):
                    continue
                for name, version in deps.items():
                    if not isinstance(version, str):
                        continue
                    normalized = version.strip().lower()
                    if normalized in {"*", "latest", "x"}:
                        bad_versions.append(name)

            if bad_versions:
                add_finding(
                    "medium",
                    "Loose Node dependency versions",
                    f"Avoid wildcard/latest versions for: {', '.join(bad_versions[:8])}.",
                    "package.json",
                )

    if todo_hits > 20:
        add_finding(
            "low",
            "High TODO/FIXME count",
            f"Found {todo_hits} TODO/FIXME markers. Consider cleaning up pending work notes.",
        )

    if large_files:
        sample = ", ".join(name for name, _ in large_files[:5])
        add_finding(
            "low",
            "Large files in repository",
            f"Detected {len(large_files)} files larger than 2MB (examples: {sample}).",
        )

    severity_weight = {"high": 15, "medium": 8, "low": 3}
    score = 100
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        sev = finding["severity"]
        counts[sev] = counts.get(sev, 0) + 1
        score -= severity_weight.get(sev, 0)
    score = max(0, score)

    findings.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3))

    return {
        "score": score,
        "summary": {
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
            "scanned_files": scanned_files,
            "todo_fixme_count": todo_hits,
        },
        "findings": findings,
        "meta": {
            "quick_mode": quick_mode,
            "max_findings": max_findings,
        },
    }


def docker_build_and_run(repo_path: Path, image_name: str, container_port: int) -> dict[str, Any]:
    host_port = random.randint(8000, 9000)
    image_repo = image_name.split(":", 1)[0]
    safe_container_base = docker_safe_name(image_repo)
    container_name = f"{safe_container_base}-{host_port}"[:63]

    build = run_command(f"docker build -t {image_name} .", repo_path, timeout=1800)
    if not build.ok:
        raise RuntimeError(build.stderr or build.stdout or "Docker build failed")

    run = run_command(
        f"docker run -d -p {host_port}:{container_port} --name {container_name} {image_name}",
        repo_path,
    )
    if not run.ok:
        raise RuntimeError(run.stderr or run.stdout or "Docker run failed")

    time.sleep(2)
    running_check = run_command(
        f'docker inspect -f "{{{{.State.Running}}}}" {container_name}',
        repo_path,
    )
    running_value = (running_check.stdout or "").strip().lower()
    if running_value != "true":
        logs = run_command(f"docker logs {container_name}", repo_path)
        raise RuntimeError(
            "Container exited immediately after startup. "
            f"Container logs:\n{logs.stdout or logs.stderr or 'No logs available'}"
        )

    return {
        "host_port": host_port,
        "container_port": container_port,
        "container_name": container_name,
        "container_id": run.stdout.strip(),
        "preview_url": f"http://localhost:{host_port}",
        "build_logs": build.stdout,
    }


def deploy_to_platform(platform: str, repo_path: Path) -> dict[str, Any]:
    platform_l = platform.strip().lower()
    if platform_l == "render":
        return {
            "platform": "render",
            "status": "manual-step-required",
            "message": "Use Render dashboard or render CLI after linking this repository.",
        }
    if platform_l == "railway":
        if shutil.which("railway") is None:
            return {
                "platform": "railway",
                "status": "blocked",
                "message": "Railway CLI not found on host machine.",
            }
        cmd = run_command("railway up", repo_path, timeout=1800)
        return {
            "platform": "railway",
            "status": "success" if cmd.ok else "failed",
            "logs": cmd.stdout or cmd.stderr,
        }
    if platform_l == "flyio":
        if shutil.which("flyctl") is None:
            return {
                "platform": "flyio",
                "status": "blocked",
                "message": "flyctl not found on host machine.",
            }
        cmd = run_command("flyctl deploy", repo_path, timeout=1800)
        return {
            "platform": "flyio",
            "status": "success" if cmd.ok else "failed",
            "logs": cmd.stdout or cmd.stderr,
        }

    return {
        "platform": platform,
        "status": "not-supported",
        "message": "Supported platforms: render, railway, flyio",
    }


@app.route("/review-repo", methods=["POST"])
def review_repo() -> Any:
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()
    quick_mode = bool(data.get("quick_mode", True))
    max_findings = int(data.get("max_findings", 200))
    if max_findings < 20:
        max_findings = 20
    if max_findings > 1000:
        max_findings = 1000

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    head_sha = get_repo_head_sha(repo_path)
    cache_key = f"{repo_path}:{head_sha}:{project_type}:{int(quick_mode)}:{max_findings}"

    with LOCK:
        cached = REVIEW_CACHE.get(cache_key)

    if cached:
        return jsonify(
            {
                "status": "reviewed",
                "repo": repo_name,
                "repo_path": str(repo_path),
                "project_type": project_type,
                "clone_logs": clone_logs,
                "review": cached,
                "from_cache": True,
                "head_sha": head_sha,
                "message": "CodeRabbit-style automated review completed (cached).",
            }
        )

    started = time.time()
    review = run_repo_review_checks(
        repo_path,
        project_type,
        quick_mode=quick_mode,
        max_findings=max_findings,
    )
    review["meta"] = {
        **review.get("meta", {}),
        "duration_ms": int((time.time() - started) * 1000),
    }

    with LOCK:
        REVIEW_CACHE[cache_key] = review

    return jsonify(
        {
            "status": "reviewed",
            "repo": repo_name,
            "repo_path": str(repo_path),
            "project_type": project_type,
            "clone_logs": clone_logs,
            "review": review,
            "from_cache": False,
            "head_sha": head_sha,
            "message": "CodeRabbit-style automated review completed.",
        }
    )


@app.route("/orchestrate", methods=["POST"])
def orchestrate() -> Any:
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()
    env_values = data.get("env_values") or {}
    auto_fill_env = bool(data.get("auto_fill_env", False))
    force_rebuild = bool(data.get("force_rebuild", False))

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400
    if not isinstance(env_values, dict):
        return jsonify({"error": "env_values must be an object/dictionary"}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    agents = run_project_agents(repo_path)
    project_type = agents.get("project_type_agent", {}).get("project_type", "unknown")
    required_env_keys = agents.get("env_agent", {}).get("required_env_keys", [])

    if project_type == "unknown":
        return jsonify(
            {
                "status": "failed",
                "message": "Unsupported project type. Only Python and Node projects are supported right now.",
                "repo": repo_name,
                "agents": agents,
                "clone_logs": clone_logs,
            }
        ), 400

    missing_keys = [key for key in required_env_keys if key not in env_values]
    if required_env_keys and missing_keys and not auto_fill_env:
        return jsonify(
            {
                "status": "env-required",
                "repo": repo_name,
                "required_env_keys": required_env_keys,
                "missing_env_keys": missing_keys,
                "message": "Provide env_values for missing keys, or call again with auto_fill_env=true to create placeholders.",
                "agents": agents,
            }
        ), 200

    env_file = write_env_file(repo_path, env_values, required_env_keys)
    write_env_example(repo_path, required_env_keys)
    ensure_gitignore_has_env(repo_path)

    framework_details = agents.get("framework_agent", {}).get("framework_details", {})
    framework = framework_details.get("framework", "unknown")

    try:
        dockerfile_path, docker_changed, container_port, docker_note = generate_dockerfile(repo_path, project_type)
    except Exception as exc:
        return jsonify({"error": f"Dockerfile step failed: {exc}"}), 500

    # Agent 5: Build validation
    build_validation = validate_build(repo_path, project_type, framework)

    image_name = f"{docker_safe_name(repo_name)}:latest"

    if force_rebuild:
        _ = run_command(f"docker rmi {image_name}", repo_path)

    try:
        docker_result = docker_build_and_run(repo_path, image_name, container_port)
    except Exception as exc:
        return jsonify(
            {
                "status": "failed",
                "error": str(exc),
                "repo": repo_name,
                "project_type": project_type,
                "dockerfile": str(dockerfile_path),
            }
        ), 500

    session_id = f"sess-{int(time.time())}-{random.randint(1000, 9999)}"
    with LOCK:
        SESSIONS[session_id] = {
            "repo_name": repo_name,
            "repo_path": str(repo_path),
            "image_name": image_name,
            "container": docker_result,
            "project_type": project_type,
        }

    return jsonify(
        {
            "status": "ready",
            "session_id": session_id,
            "repo": repo_name,
            "repo_path": str(repo_path),
            "project_type": project_type,
            "framework_details": framework_details,
            "agents": agents,
            "clone_logs": clone_logs,
            "env_file": str(env_file),
            "build_validation": build_validation,
            "dockerfile": str(dockerfile_path),
            "docker_changed": docker_changed,
            "docker_note": docker_note,
            "container": docker_result,
            "next": "If satisfied, call POST /deploy-platform with {session_id, user_satisfied:true, platform:'render|railway|flyio'}",
        }
    )


@app.route("/deploy-platform", methods=["POST"])
def deploy_platform() -> Any:
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    user_satisfied = bool(data.get("user_satisfied", False))
    platform = (data.get("platform") or "").strip()

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    with LOCK:
        session = SESSIONS.get(session_id)

    if not session:
        return jsonify({"error": "Invalid or expired session_id"}), 404

    if not user_satisfied:
        return jsonify(
            {
                "status": "pending-user-feedback",
                "message": "User not satisfied yet. Make project changes first, then call again with user_satisfied=true.",
                "repo": session["repo_name"],
            }
        ), 200

    if not platform:
        return jsonify({"error": "platform is required when user_satisfied=true"}), 400

    result = deploy_to_platform(platform, Path(session["repo_path"]))
    return jsonify(
        {
            "status": "deployment-attempted",
            "session_id": session_id,
            "repo": session["repo_name"],
            "result": result,
        }
    )


@app.route("/health", methods=["GET"])
def health() -> Any:
    return jsonify({"status": "ok", "sessions": len(SESSIONS)})


@app.route("/analyze", methods=["POST"])
def analyze() -> Any:
    """Agent 1: Repository Clone & Structure Analysis."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    env_keys = detect_env_keys(repo_path)
    dep_validation = validate_dependencies(repo_path, project_type)

    exclude_dirs = {".git", "node_modules", "venv", "__pycache__", "dist", "build"}
    file_counts: dict[str, int] = {}
    for f in repo_path.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(repo_path)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        ext = f.suffix.lower() or "(no ext)"
        file_counts[ext] = file_counts.get(ext, 0) + 1

    config_files = {
        "requirements.txt": (repo_path / "requirements.txt").exists(),
        "package.json": (repo_path / "package.json").exists(),
        "Dockerfile": (repo_path / "Dockerfile").exists(),
        "README.md": (repo_path / "README.md").exists(),
        ".env.example": (repo_path / ".env.example").exists(),
        ".gitignore": (repo_path / ".gitignore").exists(),
        "docker-compose.yml": (repo_path / "docker-compose.yml").exists(),
    }
    primary_dep_file = "requirements.txt" if project_type == "python" else "package.json"
    missing_critical = [name for name, present in config_files.items() if not present and name in {primary_dep_file, "Dockerfile", "README.md"}]

    return jsonify({
        "status": "analyzed",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "framework_details": framework_details,
        "required_env_keys": env_keys,
        "config_files": config_files,
        "missing_critical_files": missing_critical,
        "dependency_validation": dep_validation,
        "file_counts": dict(sorted(file_counts.items(), key=lambda x: -x[1])[:15]),
    })


@app.route("/generate-readme", methods=["POST"])
def generate_readme_endpoint() -> Any:
    """Agent 9: Generate a professional README.md for the repository."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()
    write_to_file = bool(data.get("write_to_file", False))

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    env_keys = detect_env_keys(repo_path)
    framework = framework_details.get("framework", "unknown")
    build_info = validate_build(repo_path, project_type, framework)
    readme_content = generate_readme_content(repo_path, project_type, framework, env_keys, build_info)

    result: dict[str, Any] = {
        "status": "generated",
        "repo": repo_name,
        "readme": readme_content,
    }

    if write_to_file:
        readme_path = repo_path / "README.md"
        existed = readme_path.exists()
        readme_path.write_text(readme_content, encoding="utf-8")
        result["written_to"] = str(readme_path)
        result["overwritten"] = existed

    return jsonify(result)


@app.route("/improvement-report", methods=["POST"])
def improvement_report() -> Any:
    """Agent 10: Generate a comprehensive repository improvement report."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    framework = framework_details.get("framework", "unknown")
    env_keys = detect_env_keys(repo_path)
    dep_validation = validate_dependencies(repo_path, project_type)
    build_validation = validate_build(repo_path, project_type, framework)
    review = run_repo_review_checks(repo_path, project_type, quick_mode=True)

    findings = review.get("findings") or []
    return jsonify({
        "status": "report-generated",
        "repo": repo_name,
        "project_type": project_type,
        "framework_details": framework_details,
        "dependency_validation": dep_validation,
        "build_validation": build_validation,
        "code_review": review,
        "required_env_keys": env_keys,
        "recommendations": {
            "critical": [f for f in findings if f["severity"] == "high"],
            "moderate": [f for f in findings if f["severity"] == "medium"],
            "minor": [f for f in findings if f["severity"] == "low"],
        },
        "overall_score": review.get("score", 0),
    })


# ---------------------------------------------------------------------------
# NEW AGENT 1 — SELF-FIXING REPOSITORY AGENT
# ---------------------------------------------------------------------------

def apply_repo_fixes(repo_path: Path, project_type: str) -> list[dict[str, str]]:
    """Automatically apply common fixes to the repository and return a list of applied fixes."""
    fixes: list[dict[str, str]] = []

    # Fix 1: Add .env.example if missing
    env_example = repo_path / ".env.example"
    if not env_example.exists():
        env_keys = detect_env_keys(repo_path)
        write_env_example(repo_path, env_keys)
        fixes.append({
            "fix": "added_env_example",
            "file": ".env.example",
            "description": "Generated .env.example documenting detected environment variables.",
        })

    # Fix 2: Generate .gitignore if missing or ensure .env is excluded
    gitignore = repo_path / ".gitignore"
    if not gitignore.exists():
        default_entries = [".env", "__pycache__/", "*.pyc", "node_modules/", "dist/", "build/", ".DS_Store"]
        gitignore.write_text("\n".join(default_entries) + "\n", encoding="utf-8")
        fixes.append({
            "fix": "generated_gitignore",
            "file": ".gitignore",
            "description": "Generated default .gitignore with common exclusion patterns.",
        })
    else:
        modified = ensure_gitignore_has_env(repo_path)
        if modified:
            fixes.append({
                "fix": "updated_gitignore",
                "file": ".gitignore",
                "description": "Added .env to .gitignore to prevent secret leakage.",
            })

    # Fix 3: Fix / generate Dockerfile if missing or incomplete
    dockerfile = repo_path / "Dockerfile"
    if not dockerfile.exists() and project_type in ("python", "node"):
        try:
            _, changed, _, note = generate_dockerfile(repo_path, project_type)
            if changed:
                fixes.append({
                    "fix": "generated_dockerfile",
                    "file": "Dockerfile",
                    "description": f"Generated Dockerfile for {project_type} project.",
                })
        except Exception:
            pass
    elif dockerfile.exists():
        content = dockerfile.read_text(encoding="utf-8", errors="ignore")
        patched = False
        if "EXPOSE" not in content.upper():
            port = 5000 if project_type == "python" else 3000
            content += f"\nEXPOSE {port}\n"
            patched = True
        if "CMD" not in content.upper():
            if project_type == "python":
                entry = find_python_entrypoint(repo_path)
                content += f'CMD ["python", "{entry}"]\n'
            else:
                content += 'CMD ["npm", "start"]\n'
            patched = True
        if patched:
            dockerfile.write_text(content, encoding="utf-8")
            fixes.append({
                "fix": "repaired_dockerfile",
                "file": "Dockerfile",
                "description": "Added missing EXPOSE/CMD directives to existing Dockerfile.",
            })

    # Fix 4: Fix package.json scripts for Node.js projects
    if project_type == "node":
        pkg_json_path = repo_path / "package.json"
        if pkg_json_path.exists():
            try:
                data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
                scripts = data.get("scripts", {})
                if not isinstance(scripts, dict):
                    scripts = {}
                changed_pkg = False
                if "start" not in scripts:
                    main = data.get("main", "index.js")
                    scripts["start"] = f"node {main}"
                    changed_pkg = True
                if changed_pkg:
                    data["scripts"] = scripts
                    pkg_json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                    fixes.append({
                        "fix": "fixed_package_json_scripts",
                        "file": "package.json",
                        "description": "Added missing 'start' script to package.json.",
                    })
            except Exception:
                pass

    # Fix 5: Correct Python entrypoint — ensure app.py / main.py exists
    if project_type == "python":
        py_files = list(repo_path.glob("*.py"))
        preferred = ["app.py", "main.py", "run.py", "wsgi.py"]
        has_preferred = any((repo_path / p).exists() for p in preferred)
        if not has_preferred and py_files:
            # Create a minimal main.py that imports the first found module
            first_module = py_files[0].stem
            main_py = repo_path / "main.py"
            if not main_py.exists():
                main_py.write_text(
                    f"# Auto-generated entrypoint — customize this file for your application.\n"
                    f"import {first_module}  # noqa: F401\n",
                    encoding="utf-8",
                )
                fixes.append({
                    "fix": "created_python_entrypoint",
                    "file": "main.py",
                    "description": f"Created main.py entrypoint referencing {first_module}.",
                })

    # Fix 6: Add requirements.txt for Python projects if missing
    if project_type == "python":
        req = repo_path / "requirements.txt"
        if not req.exists():
            req.write_text("# Add your Python dependencies here\n", encoding="utf-8")
            fixes.append({
                "fix": "created_requirements_txt",
                "file": "requirements.txt",
                "description": "Created empty requirements.txt placeholder for Python dependencies.",
            })

    return fixes


@app.route("/fix-repo", methods=["POST"])
def fix_repo() -> Any:
    """New Agent 1: Self-Fixing Repository Agent — automatically apply common project fixes."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    fixes = apply_repo_fixes(repo_path, project_type)

    return jsonify({
        "status": "fixed",
        "repo": repo_name,
        "project_type": project_type,
        "clone_logs": clone_logs,
        "fixes_applied": fixes,
        "fix_count": len(fixes),
        "message": f"Applied {len(fixes)} fix(es) to the repository.",
    })


# ---------------------------------------------------------------------------
# NEW AGENT 2 — AUTOMATED PULL REQUEST AGENT
# ---------------------------------------------------------------------------

def build_pr_payload(repo_name: str, fixes: list[dict[str, str]]) -> dict[str, Any]:
    """Build a structured PR description from applied fixes."""
    if not fixes:
        return {
            "title": "chore: automated repository audit (no changes required)",
            "description": "No fixes were necessary. The repository already meets baseline standards.",
            "summary": [],
            "modified_files": [],
        }

    modified_files = sorted({f["file"] for f in fixes})
    fix_labels = {
        "added_env_example": "Added `.env.example` for environment variable documentation",
        "generated_gitignore": "Generated `.gitignore` with common exclusion patterns",
        "updated_gitignore": "Updated `.gitignore` to exclude `.env` files",
        "generated_dockerfile": "Generated `Dockerfile` for containerized deployment",
        "repaired_dockerfile": "Repaired `Dockerfile` (added missing EXPOSE/CMD directives)",
        "fixed_package_json_scripts": "Fixed `package.json` scripts (added missing `start` entry)",
        "created_python_entrypoint": "Created Python entrypoint (`main.py`)",
        "created_requirements_txt": "Created `requirements.txt` placeholder",
    }

    summary = [fix_labels.get(f["fix"], f["description"]) for f in fixes]
    body_lines = "\n".join(f"- {s}" for s in summary)
    files_line = "\n".join(f"- `{f}`" for f in modified_files)

    description = (
        f"## Summary\n\n"
        f"Automated fixes applied by the Cloud Agent self-repair system.\n\n"
        f"## Changes\n\n{body_lines}\n\n"
        f"## Modified Files\n\n{files_line}\n\n"
        f"## Notes\n\n"
        f"These changes were generated automatically. Please review before merging."
    )

    return {
        "title": f"fix: automated repository repairs for {repo_name}",
        "description": description,
        "summary": summary,
        "modified_files": modified_files,
    }


@app.route("/prepare-pr", methods=["POST"])
def prepare_pr() -> Any:
    """New Agent 2: Automated Pull Request Agent — generate PR metadata after fixes."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    fixes = apply_repo_fixes(repo_path, project_type)
    pr = build_pr_payload(repo_name, fixes)

    diff_output = ""
    try:
        diff_result = run_command("git diff --stat HEAD", repo_path, timeout=30)
        diff_output = diff_result.stdout or diff_result.stderr or ""
    except Exception:
        pass

    return jsonify({
        "status": "pr-ready",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "pr": pr,
        "fixes_applied": fixes,
        "git_diff_stat": diff_output,
        "message": "Pull request metadata prepared. Review and submit via your Git provider.",
    })


# ---------------------------------------------------------------------------
# NEW AGENT 3 — ARCHITECTURE VISUALIZATION AGENT
# ---------------------------------------------------------------------------

def _detect_database_tech(content_lower: str) -> list[str]:
    """Return database technologies detected from source content."""
    db_map = {
        "mongodb": "MongoDB",
        "mongoose": "MongoDB",
        "postgres": "PostgreSQL",
        "psycopg": "PostgreSQL",
        "mysql": "MySQL",
        "pymysql": "MySQL",
        "redis": "Redis",
        "sqlite": "SQLite",
        "sqlalchemy": "SQLAlchemy/SQL",
        "prisma": "Prisma ORM",
        "sequelize": "Sequelize ORM",
    }
    return [label for token, label in db_map.items() if token in content_lower]


def _detect_external_integrations(content_lower: str) -> list[str]:
    """Return external service integrations detected from source content."""
    ext_map = {
        "stripe": "Stripe",
        "twilio": "Twilio",
        "sendgrid": "SendGrid",
        "firebase": "Firebase",
        "aws": "AWS",
        "s3": "AWS S3",
        "openai": "OpenAI API",
        "anthropic": "Anthropic API",
        "oauth": "OAuth",
        "auth0": "Auth0",
    }
    return [label for token, label in ext_map.items() if token in content_lower]


def generate_architecture_diagram(repo_path: Path, project_type: str, framework: str) -> dict[str, Any]:
    """Analyse repository structure and generate a Mermaid architecture diagram."""
    layers: dict[str, list[str]] = {
        "frontend": [],
        "backend": [],
        "database": [],
        "external": [],
    }

    exclude_dirs = {".git", "node_modules", "venv", "__pycache__", "dist", "build", ".next"}

    all_content = ""
    for f in repo_path.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(repo_path)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        if f.suffix.lower() not in {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".env.example"}:
            continue
        try:
            all_content += f.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue

    # Detect frontend
    if framework in ("react",):
        subtype = ""
        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "next" in deps:
                    subtype = " / Next.js"
                elif "vite" in deps:
                    subtype = " / Vite"
            except Exception:
                pass
        layers["frontend"].append(f"React App{subtype}")
    elif "vue" in all_content:
        layers["frontend"].append("Vue.js App")
    elif "angular" in all_content:
        layers["frontend"].append("Angular App")

    # Detect backend
    backend_map = {
        "express": "Express API",
        "fastify": "Fastify API",
        "nestjs": "NestJS API",
        "fastapi": "FastAPI",
        "flask": "Flask API",
        "django": "Django API",
        "streamlit": "Streamlit UI",
    }
    for fw, label in backend_map.items():
        if fw == framework:
            layers["backend"].append(label)
            break
    if not layers["backend"] and project_type in ("python", "node"):
        layers["backend"].append(f"{project_type.title()} Service")

    # Detect databases & externals
    layers["database"] = _detect_database_tech(all_content)
    layers["external"] = _detect_external_integrations(all_content)

    # Build Mermaid diagram
    nodes: list[str] = []
    edges: list[str] = []

    frontend_ids: list[str] = []
    for i, fe in enumerate(layers["frontend"]):
        nid = f"FE{i}"
        nodes.append(f'    {nid}["{fe}"]')
        frontend_ids.append(nid)

    backend_ids: list[str] = []
    for i, be in enumerate(layers["backend"]):
        nid = f"BE{i}"
        nodes.append(f'    {nid}["{be}"]')
        backend_ids.append(nid)

    db_ids: list[str] = []
    for i, db in enumerate(layers["database"]):
        nid = f"DB{i}"
        nodes.append(f'    {nid}[("{db}")]')
        db_ids.append(nid)

    ext_ids: list[str] = []
    for i, ext in enumerate(layers["external"]):
        nid = f"EXT{i}"
        nodes.append(f'    {nid}["{ext}"]')
        ext_ids.append(nid)

    for fid in frontend_ids:
        for bid in backend_ids:
            edges.append(f"    {fid} --> {bid}")

    for bid in backend_ids:
        for did in db_ids:
            edges.append(f"    {bid} --> {did}")
        for eid in ext_ids:
            edges.append(f"    {bid} --> {eid}")

    if not nodes:
        nodes.append('    APP["Application"]')

    diagram_lines = ["graph TD"] + nodes + edges
    mermaid_diagram = "\n".join(diagram_lines)

    return {
        "layers": layers,
        "mermaid_diagram": mermaid_diagram,
    }


@app.route("/visualize-architecture", methods=["POST"])
def visualize_architecture() -> Any:
    """New Agent 3: Architecture Visualization Agent — generate Mermaid diagrams."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    framework = framework_details.get("framework", "unknown")
    arch = generate_architecture_diagram(repo_path, project_type, framework)

    return jsonify({
        "status": "visualized",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "framework": framework,
        "architecture": arch,
        "message": "Architecture diagram generated. Embed mermaid_diagram in your README.",
    })


# ---------------------------------------------------------------------------
# NEW AGENT 4 — PERFORMANCE ANALYSIS AGENT
# ---------------------------------------------------------------------------

def analyze_performance(repo_path: Path, project_type: str, framework: str) -> list[dict[str, str]]:
    """Scan repository for common performance issues and return recommendations."""
    issues: list[dict[str, str]] = []

    exclude_dirs = {".git", "node_modules", "venv", "__pycache__", "dist", "build", ".next"}

    for f in repo_path.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(repo_path)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        if f.suffix.lower() not in {".py", ".js", ".ts", ".jsx", ".tsx"}:
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel_str = str(rel)

        # Python: blocking sleep in request handlers
        if project_type == "python" and "time.sleep(" in content:
            issues.append({
                "file": rel_str,
                "issue": "Blocking sleep detected",
                "suggestion": "Replace time.sleep() with async equivalents (asyncio.sleep) or background tasks.",
                "severity": "medium",
            })

        # Python: nested loops that could be expensive
        if project_type == "python":
            nested = re.findall(r"for .+ in .+:\s*\n\s+for .+ in .+:", content)
            if nested:
                issues.append({
                    "file": rel_str,
                    "issue": "Nested loops detected",
                    "suggestion": "Review nested loops for O(n²) complexity; consider vectorised operations (numpy) or dict lookups.",
                    "severity": "low",
                })

        # React: inline function creation in JSX (unnecessary re-renders)
        if framework == "react" and f.suffix.lower() in {".jsx", ".tsx", ".js", ".ts"}:
            inline_fns = re.findall(r"on\w+\s*=\s*\{?\s*\(", content)
            if len(inline_fns) > 3:
                issues.append({
                    "file": rel_str,
                    "issue": "Multiple inline arrow functions in JSX event handlers",
                    "suggestion": "Extract handlers to useCallback to prevent unnecessary child re-renders.",
                    "severity": "low",
                })

        # Node/React: synchronous fs calls
        if project_type == "node" and re.search(r"\bfs\.(readFileSync|writeFileSync|existsSync)\b", content):
            issues.append({
                "file": rel_str,
                "issue": "Synchronous file system call detected",
                "suggestion": "Use async fs.promises API or streams to avoid blocking the event loop.",
                "severity": "medium",
            })

    # React: check bundle size indicators
    if framework == "react":
        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                heavy = [d for d in ("moment", "lodash", "rxjs", "antd", "material-ui", "@mui/material") if d in deps]
                if heavy:
                    issues.append({
                        "file": "package.json",
                        "issue": f"Heavy dependencies detected: {', '.join(heavy)}",
                        "suggestion": (
                            "Use tree-shakable alternatives (e.g. date-fns instead of moment, "
                            "lodash-es for tree-shaking) and enable code splitting / lazy loading."
                        ),
                        "severity": "medium",
                    })
                if "react-router-dom" in deps:
                    src_extensions = {".jsx", ".tsx", ".js", ".ts"}
                    src_content = "".join(
                        f.read_text(encoding="utf-8", errors="ignore")
                        for f in repo_path.rglob("*")
                        if f.is_file() and f.suffix.lower() in src_extensions
                        and not any(part in exclude_dirs for part in f.relative_to(repo_path).parts)
                    )
                    if "React.lazy" not in src_content:
                        issues.append({
                            "file": "src/",
                            "issue": "React Router detected but no lazy loading found",
                            "suggestion": "Use React.lazy() and Suspense for route-level code splitting to reduce initial bundle size.",
                            "severity": "low",
                        })
            except Exception:
                pass

    return issues


@app.route("/analyze-performance", methods=["POST"])
def analyze_performance_endpoint() -> Any:
    """New Agent 4: Performance Analysis Agent — detect performance anti-patterns."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    framework = framework_details.get("framework", "unknown")
    perf_issues = analyze_performance(repo_path, project_type, framework)

    severity_count = {"high": 0, "medium": 0, "low": 0}
    for issue in perf_issues:
        sev = issue.get("severity", "low")
        severity_count[sev] = severity_count.get(sev, 0) + 1

    return jsonify({
        "status": "performance-analyzed",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "framework": framework,
        "issues": perf_issues,
        "issue_count": len(perf_issues),
        "severity_breakdown": severity_count,
        "message": f"Performance analysis complete. Found {len(perf_issues)} potential issue(s).",
    })


# ---------------------------------------------------------------------------
# NEW AGENT 5 — SECURITY ANALYSIS AGENT
# ---------------------------------------------------------------------------

def analyze_security(repo_path: Path, project_type: str) -> list[dict[str, str]]:
    """Deep security scan beyond the basic review checks."""
    issues: list[dict[str, str]] = []
    exclude_dirs = {".git", "node_modules", "venv", "__pycache__", "dist", "build", ".next"}

    # CORS wildcard pattern
    cors_wildcard = re.compile(r"cors\s*\(\s*['\"]?\*['\"]?\s*\)|allow_origins\s*=\s*\[?\s*['\*]['\*]?\s*\]?", re.IGNORECASE)
    # Unsafe eval
    eval_pattern = re.compile(r"\beval\s*\(", re.IGNORECASE)
    # Weak JWT secret
    weak_jwt = re.compile(r"(jwt\.sign|jwt_encode|SECRET_KEY\s*=)\s*.*['\"](.{0,16})['\"]", re.IGNORECASE)
    # SQL injection risk (string format in query)
    sql_inject = re.compile(r'(execute|cursor\.execute)\s*\(\s*["\'].*%[s|d]', re.IGNORECASE)
    # Hardcoded passwords
    hardcoded_pw = re.compile(r"(?i)(password|passwd|pwd)\s*=\s*['\"][^'\"]{4,}['\"]")
    benign_markers = ("change_me", "your_", "example", "placeholder", "test", "dummy", "sample")

    for f in repo_path.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(repo_path)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        if f.suffix.lower() not in {".py", ".js", ".ts", ".jsx", ".tsx", ".env", ".json", ".yaml", ".yml"}:
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel_str = str(rel)

        if cors_wildcard.search(content):
            issues.append({
                "file": rel_str,
                "issue": "CORS allows all origins (*)",
                "recommendation": "Restrict CORS to specific trusted origins instead of using wildcard.",
                "severity": "high",
            })

        for m in eval_pattern.finditer(content):
            ctx = content[max(0, m.start() - 40):m.end() + 40].strip()
            issues.append({
                "file": rel_str,
                "issue": "Unsafe eval() usage detected",
                "recommendation": "Avoid eval(); use safer alternatives like JSON.parse() or ast.literal_eval().",
                "severity": "high",
                "context": ctx,
            })

        for m in weak_jwt.finditer(content):
            secret = m.group(2)
            if len(secret) < 16:
                issues.append({
                    "file": rel_str,
                    "issue": "Weak or short JWT secret",
                    "recommendation": "Use a cryptographically strong secret of at least 32 characters from an environment variable.",
                    "severity": "high",
                })

        for m in sql_inject.finditer(content):
            issues.append({
                "file": rel_str,
                "issue": "Potential SQL injection via string formatting",
                "recommendation": "Use parameterised queries instead of string formatting in SQL statements.",
                "severity": "high",
            })

        for m in hardcoded_pw.finditer(content):
            candidate = m.group(0).lower()
            if any(marker in candidate for marker in benign_markers):
                continue
            issues.append({
                "file": rel_str,
                "issue": "Hardcoded password detected",
                "recommendation": "Move passwords to environment variables and load via os.getenv() / process.env.",
                "severity": "high",
            })

    # Check for .env committed to repo (non-example)
    env_file = repo_path / ".env"
    if env_file.exists():
        gitignore = repo_path / ".gitignore"
        ignored = False
        if gitignore.exists():
            try:
                content = gitignore.read_text(encoding="utf-8", errors="ignore")
                gitignore_lines = content.splitlines()
                ignored = ".env" in gitignore_lines or "*.env" in gitignore_lines
            except OSError:
                pass
        if not ignored:
            issues.append({
                "file": ".env",
                "issue": ".env file present but may not be in .gitignore",
                "recommendation": "Ensure .env is listed in .gitignore to prevent committing secrets.",
                "severity": "high",
            })

    # Node.js: check for npm audit advisories indicator
    if project_type == "node":
        pkg_lock = repo_path / "package-lock.json"
        if pkg_lock.exists():
            try:
                audit = run_command("npm audit --json --audit-level=high", repo_path, timeout=120)
                if audit.stdout:
                    try:
                        audit_data = json.loads(audit.stdout)
                        vuln_count = audit_data.get("metadata", {}).get("vulnerabilities", {})
                        high_vulns = vuln_count.get("high", 0) + vuln_count.get("critical", 0)
                        if high_vulns > 0:
                            issues.append({
                                "file": "package-lock.json",
                                "issue": f"npm audit reports {high_vulns} high/critical vulnerability(s)",
                                "recommendation": "Run `npm audit fix` or update affected packages.",
                                "severity": "high",
                            })
                    except Exception:
                        pass
            except Exception:
                pass

    return issues


@app.route("/analyze-security", methods=["POST"])
def analyze_security_endpoint() -> Any:
    """New Agent 5: Security Analysis Agent — deep security inspection."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    sec_issues = analyze_security(repo_path, project_type)
    severity_count = {"high": 0, "medium": 0, "low": 0}
    for issue in sec_issues:
        sev = issue.get("severity", "low")
        severity_count[sev] = severity_count.get(sev, 0) + 1

    return jsonify({
        "status": "security-analyzed",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "issues": sec_issues,
        "issue_count": len(sec_issues),
        "severity_breakdown": severity_count,
        "message": f"Security analysis complete. Found {len(sec_issues)} potential issue(s).",
    })


# ---------------------------------------------------------------------------
# NEW AGENT 6 — DEPENDENCY CLEANUP AGENT
# ---------------------------------------------------------------------------

def find_unused_dependencies(repo_path: Path, project_type: str) -> dict[str, Any]:
    """Scan source code imports vs declared dependencies to find unused packages."""
    unused: list[str] = []
    declared: list[str] = []
    used_imports: list[str] = []

    exclude_dirs = {".git", "node_modules", "venv", "__pycache__", "dist", "build", ".next"}

    if project_type == "python":
        req_file = repo_path / "requirements.txt"
        if not req_file.exists():
            return {"declared": [], "used": [], "unused": [], "note": "requirements.txt not found"}

        try:
            req_lines = req_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return {"declared": [], "used": [], "unused": [], "note": "Could not read requirements.txt"}

        for line in req_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            # Normalise package name (strip version specifiers)
            pkg = re.split(r"[>=<!~\[]", stripped)[0].strip().lower().replace("-", "_")
            if pkg:
                declared.append(pkg)

        # Collect all import statements from Python files
        import_pattern = re.compile(r"^\s*(?:import|from)\s+([a-zA-Z0-9_]+)", re.MULTILINE)
        for f in repo_path.rglob("*.py"):
            rel = f.relative_to(repo_path)
            if any(part in exclude_dirs for part in rel.parts):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in import_pattern.finditer(content):
                used_imports.append(m.group(1).lower().replace("-", "_"))

        used_set = set(used_imports)
        unused = [pkg for pkg in declared if pkg not in used_set]

    elif project_type == "node":
        pkg_json = repo_path / "package.json"
        if not pkg_json.exists():
            return {"declared": [], "used": [], "unused": [], "note": "package.json not found"}

        try:
            pkg_data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except Exception:
            return {"declared": [], "used": [], "unused": [], "note": "Could not parse package.json"}

        deps = list(pkg_data.get("dependencies", {}).keys())
        declared = [d.lower() for d in deps]

        # Collect require/import statements
        require_pattern = re.compile(r"""(?:require|import)\s*\(*['"]([^'"./][^'"]*?)['"]""")
        for f in repo_path.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(repo_path)
            if any(part in exclude_dirs for part in rel.parts):
                continue
            if f.suffix.lower() not in {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}:
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in require_pattern.finditer(content):
                raw = m.group(1)
                # Handle scoped packages (@scope/pkg)
                if raw.startswith("@"):
                    parts = raw.split("/")
                    used_imports.append("/".join(parts[:2]).lower())
                else:
                    used_imports.append(raw.split("/")[0].lower())

        used_set = set(used_imports)
        unused = [pkg for pkg in declared if pkg not in used_set]

    return {
        "declared": declared,
        "used": sorted(set(used_imports)),
        "unused": unused,
        "note": f"Found {len(unused)} potentially unused package(s) out of {len(declared)} declared.",
    }


@app.route("/cleanup-dependencies", methods=["POST"])
def cleanup_dependencies() -> Any:
    """New Agent 6: Dependency Cleanup Agent — detect unused packages."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    result = find_unused_dependencies(repo_path, project_type)

    return jsonify({
        "status": "dependencies-analyzed",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "dependency_analysis": result,
        "message": result.get("note", ""),
    })


# ---------------------------------------------------------------------------
# NEW AGENT 7 — TEST GENERATION AGENT
# ---------------------------------------------------------------------------

def generate_starter_tests(repo_path: Path, project_type: str, framework: str) -> list[dict[str, str]]:
    """Generate starter test files for the project."""
    generated: list[dict[str, str]] = []

    if project_type == "python":
        test_dir = repo_path / "tests"
        test_file = test_dir / "test_app.py"
        if test_file.exists():
            return []
        test_dir.mkdir(exist_ok=True)

        if framework in ("flask", "fastapi"):
            if framework == "flask":
                content = (
                    '"""Auto-generated Flask starter tests."""\n'
                    "import pytest\n\n"
                    "try:\n"
                    "    from app import app as flask_app\n"
                    "except ImportError:\n"
                    "    flask_app = None\n\n\n"
                    "@pytest.fixture()\n"
                    "def client():\n"
                    "    if flask_app is None:\n"
                    "        pytest.skip('app module not importable')\n"
                    "    flask_app.config['TESTING'] = True\n"
                    "    with flask_app.test_client() as c:\n"
                    "        yield c\n\n\n"
                    "def test_health(client):\n"
                    "    response = client.get('/health')\n"
                    "    assert response.status_code in (200, 404)\n\n\n"
                    "def test_index(client):\n"
                    "    response = client.get('/')\n"
                    "    assert response.status_code in (200, 301, 302)\n"
                )
            else:  # fastapi
                content = (
                    '"""Auto-generated FastAPI starter tests."""\n'
                    "import pytest\n"
                    "from fastapi.testclient import TestClient\n\n"
                    "try:\n"
                    "    from app import app\n"
                    "except ImportError:\n"
                    "    app = None\n\n\n"
                    "@pytest.fixture()\n"
                    "def client():\n"
                    "    if app is None:\n"
                    "        pytest.skip('app module not importable')\n"
                    "    return TestClient(app)\n\n\n"
                    "def test_health(client):\n"
                    "    response = client.get('/health')\n"
                    "    assert response.status_code in (200, 404)\n"
                )
        else:
            entry = find_python_entrypoint(repo_path)
            module = entry.replace(".py", "")
            content = (
                '"""Auto-generated pytest starter tests."""\n'
                "import pytest\n\n\n"
                f"def test_module_importable():\n"
                f"    \"\"\"Verify that the main module can be imported without errors.\"\"\"\n"
                f"    try:\n"
                f"        import {module}  # noqa: F401\n"
                f"    except ImportError as exc:\n"
                f"        pytest.skip(f'Module not importable: {{exc}}')\n\n\n"
                "def test_placeholder():\n"
                "    \"\"\"Placeholder test — replace with real assertions.\"\"\"\n"
                "    assert True\n"
            )

        test_file.write_text(content, encoding="utf-8")
        generated.append({"file": str(test_file.relative_to(repo_path)), "framework": "pytest"})

        # Add conftest.py
        conftest = test_dir / "conftest.py"
        if not conftest.exists():
            conftest.write_text(
                '"""pytest configuration file."""\n'
                "import sys\n"
                "from pathlib import Path\n\n"
                "# Ensure repo root is on the path so test imports work.\n"
                "sys.path.insert(0, str(Path(__file__).parent.parent))\n",
                encoding="utf-8",
            )
            generated.append({"file": str(conftest.relative_to(repo_path)), "framework": "pytest"})

    elif project_type == "node":
        # Determine test framework from package.json
        pkg_json = repo_path / "package.json"
        test_fw = "jest"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                dev_deps = pkg.get("devDependencies", {})
                if "vitest" in dev_deps:
                    test_fw = "vitest"
                elif "mocha" in dev_deps:
                    test_fw = "mocha"
            except Exception:
                pass

        if framework == "react":
            test_dir = repo_path / "src" / "__tests__"
            test_file = test_dir / "App.test.jsx"
            if not test_file.exists():
                test_dir.mkdir(parents=True, exist_ok=True)
                if test_fw == "vitest":
                    content = (
                        "import { describe, it, expect } from 'vitest';\n"
                        "import { render } from '@testing-library/react';\n\n"
                        "// Auto-generated React component test\n"
                        "describe('App', () => {\n"
                        "  it('renders without crashing', () => {\n"
                        "    // Replace App with your actual root component\n"
                        "    expect(true).toBe(true);\n"
                        "  });\n"
                        "});\n"
                    )
                else:
                    content = (
                        "import { render } from '@testing-library/react';\n\n"
                        "// Auto-generated React component test\n"
                        "describe('App', () => {\n"
                        "  it('renders without crashing', () => {\n"
                        "    // Replace App with your actual root component\n"
                        "    expect(true).toBe(true);\n"
                        "  });\n"
                        "});\n"
                    )
                test_file.write_text(content, encoding="utf-8")
                generated.append({"file": str(test_file.relative_to(repo_path)), "framework": test_fw})
        else:
            # Node.js API test
            test_dir = repo_path / "tests"
            test_file = test_dir / "app.test.js"
            if not test_file.exists():
                test_dir.mkdir(exist_ok=True)
                if test_fw == "vitest":
                    content = (
                        "import { describe, it, expect } from 'vitest';\n\n"
                        "// Auto-generated API starter tests\n"
                        "describe('API', () => {\n"
                        "  it('should be truthy placeholder', () => {\n"
                        "    expect(true).toBe(true);\n"
                        "  });\n"
                        "});\n"
                    )
                else:
                    content = (
                        "// Auto-generated API starter tests (Jest/Mocha compatible)\n"
                        "const request = require('supertest');\n\n"
                        "describe('API', () => {\n"
                        "  it('GET / should respond', async () => {\n"
                        "    // Import your app/server here and replace the placeholder\n"
                        "    expect(true).toBe(true);\n"
                        "  });\n"
                        "});\n"
                    )
                test_file.write_text(content, encoding="utf-8")
                generated.append({"file": str(test_file.relative_to(repo_path)), "framework": test_fw})

    return generated


@app.route("/generate-tests", methods=["POST"])
def generate_tests() -> Any:
    """New Agent 7: Test Generation Agent — scaffold starter tests for the project."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()
    force = bool(data.get("force", False))

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    framework = framework_details.get("framework", "unknown")

    # Check if tests already exist
    test_markers = (
        list(repo_path.glob("tests/**/*.py"))
        + list(repo_path.glob("**/test_*.py"))
        + list(repo_path.glob("**/*_test.py"))
        + list(repo_path.glob("**/__tests__/**"))
    )
    already_has_tests = bool(test_markers) and not force

    generated = [] if already_has_tests else generate_starter_tests(repo_path, project_type, framework)

    return jsonify({
        "status": "tests-generated",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "framework": framework,
        "already_had_tests": already_has_tests,
        "generated_files": generated,
        "message": (
            "Tests already present. Pass force=true to regenerate."
            if already_has_tests
            else f"Generated {len(generated)} starter test file(s)."
        ),
    })


# ---------------------------------------------------------------------------
# NEW AGENT 8 — API DISCOVERY & DOCUMENTATION AGENT
# ---------------------------------------------------------------------------

def discover_api_endpoints(repo_path: Path, project_type: str, framework: str) -> list[dict[str, Any]]:
    """Scan source files to detect API endpoint definitions."""
    endpoints: list[dict[str, Any]] = []
    exclude_dirs = {".git", "node_modules", "venv", "__pycache__", "dist", "build", ".next"}

    if framework in ("flask", "fastapi", "django", "express", "fastify", "nestjs"):
        # Flask/FastAPI route patterns
        flask_route = re.compile(
            r"""@(?:app|router|blueprint)\s*\.\s*(get|post|put|patch|delete|route)\s*\(\s*['"]([^'"]+)['"](?:[^)]*?methods\s*=\s*\[([^\]]*)\])?""",
            re.IGNORECASE,
        )
        # Express route patterns
        express_route = re.compile(
            r"""(?:app|router)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*['"]([^'"]+)['"]""",
            re.IGNORECASE,
        )

        for f in repo_path.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(repo_path)
            if any(part in exclude_dirs for part in rel.parts):
                continue
            if f.suffix.lower() not in {".py", ".js", ".ts", ".jsx", ".tsx"}:
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel_str = str(rel)

            if project_type == "python":
                for m in flask_route.finditer(content):
                    method = m.group(1).upper()
                    path = m.group(2)
                    methods_raw = m.group(3) or ""
                    # Expand methods list from @app.route(..., methods=[...])
                    if methods_raw:
                        ms = re.findall(r"['\"]([A-Z]+)['\"]", methods_raw.upper())
                        for meth in ms:
                            endpoints.append({"method": meth, "path": path, "file": rel_str})
                    else:
                        endpoints.append({"method": method if method != "ROUTE" else "GET", "path": path, "file": rel_str})

            elif project_type == "node":
                for m in express_route.finditer(content):
                    method = m.group(1).upper()
                    path = m.group(2)
                    endpoints.append({"method": method, "path": path, "file": rel_str})

    return endpoints


def format_api_docs(endpoints: list[dict[str, Any]], repo_name: str) -> str:
    """Format discovered endpoints as Markdown API documentation."""
    if not endpoints:
        return f"# {repo_name} API Documentation\n\nNo endpoints detected automatically.\n"

    lines = [
        f"# {repo_name} API Documentation\n",
        "Auto-generated by Cloud Agent API Discovery.\n",
        "## Endpoints\n",
        "| Method | Path | File |",
        "|--------|------|------|",
    ]
    for ep in endpoints:
        lines.append(f"| `{ep['method']}` | `{ep['path']}` | {ep['file']} |")

    lines += [
        "",
        "## Usage\n",
        "Base URL: `http://localhost:5000`\n",
        "All endpoints accept and return JSON unless otherwise noted.",
    ]
    return "\n".join(lines) + "\n"


@app.route("/discover-api", methods=["POST"])
def discover_api() -> Any:
    """New Agent 8: API Discovery & Documentation Agent."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()
    write_docs = bool(data.get("write_docs", False))

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    framework = framework_details.get("framework", "unknown")
    endpoints = discover_api_endpoints(repo_path, project_type, framework)
    api_docs = format_api_docs(endpoints, repo_name)

    written_to: str | None = None
    if write_docs:
        docs_path = repo_path / "API.md"
        docs_path.write_text(api_docs, encoding="utf-8")
        written_to = str(docs_path)

    return jsonify({
        "status": "api-discovered",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "framework": framework,
        "endpoints": endpoints,
        "endpoint_count": len(endpoints),
        "api_docs": api_docs,
        "written_to": written_to,
        "message": f"Discovered {len(endpoints)} API endpoint(s).",
    })


# ---------------------------------------------------------------------------
# NEW AGENT 9 — REPOSITORY HEALTH SCORING AGENT
# ---------------------------------------------------------------------------

def calculate_health_score(repo_path: Path, project_type: str, framework: str) -> dict[str, Any]:
    """Calculate a comprehensive repository health score out of 100."""
    scores: dict[str, int] = {}

    # --- Documentation (25 pts) ---
    doc_score = 0
    if (repo_path / "README.md").exists():
        readme = (repo_path / "README.md").read_text(encoding="utf-8", errors="ignore")
        if len(readme) > 200:
            doc_score += 15
        else:
            doc_score += 5
    if (repo_path / ".env.example").exists():
        doc_score += 5
    if (repo_path / "LICENSE").exists() or (repo_path / "LICENSE.md").exists():
        doc_score += 5
    scores["documentation"] = min(doc_score, 25)

    # --- Build Reliability (25 pts) ---
    build_score = 0
    if project_type == "python":
        if (repo_path / "requirements.txt").exists() or (repo_path / "pyproject.toml").exists():
            build_score += 10
        if (repo_path / "Dockerfile").exists():
            build_score += 10
        syntax = run_command("python -m compileall -q .", repo_path, timeout=120)
        if syntax.ok:
            build_score += 5
    elif project_type == "node":
        if (repo_path / "package.json").exists():
            build_score += 10
        if (repo_path / "Dockerfile").exists():
            build_score += 10
        lock_files = ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"]
        if any((repo_path / lf).exists() for lf in lock_files):
            build_score += 5
    scores["build_reliability"] = min(build_score, 25)

    # --- Security (20 pts) ---
    sec_issues = analyze_security(repo_path, project_type)
    high_sec = sum(1 for i in sec_issues if i.get("severity") == "high")
    sec_score = max(0, 20 - high_sec * 5)
    scores["security"] = min(sec_score, 20)

    # --- Testing (15 pts) ---
    test_score = 0
    if project_type == "python":
        test_files = (
            list(repo_path.glob("tests/**/*.py"))
            + list(repo_path.glob("**/test_*.py"))
            + list(repo_path.glob("**/*_test.py"))
        )
        if test_files:
            test_score = 15
    elif project_type == "node":
        test_markers = [
            repo_path / "tests",
            repo_path / "__tests__",
            repo_path / "vitest.config.js",
            repo_path / "vitest.config.ts",
            repo_path / "jest.config.js",
            repo_path / "jest.config.ts",
        ]
        if any(m.exists() for m in test_markers):
            test_score = 15
    scores["testing"] = test_score

    # --- Dependency Health (15 pts) ---
    dep_score = 15
    if project_type == "python":
        req = repo_path / "requirements.txt"
        if req.exists():
            lines = req.read_text(encoding="utf-8", errors="ignore").splitlines()
            unpinned = [line for line in lines if line.strip() and not line.strip().startswith("#") and not any(op in line for op in ("==", ">=", "<=", "~="))]
            dep_score -= min(len(unpinned) * 2, 10)
    elif project_type == "node":
        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                wild = [k for k, v in all_deps.items() if isinstance(v, str) and v.strip().lower() in ("*", "latest")]
                dep_score -= min(len(wild) * 2, 10)
            except Exception:
                pass
    scores["dependency_health"] = max(0, dep_score)

    total = sum(scores.values())
    return {
        "total_score": total,
        "max_score": 100,
        "category_scores": scores,
        "grade": "A" if total >= 85 else "B" if total >= 70 else "C" if total >= 55 else "D" if total >= 40 else "F",
    }


@app.route("/health-score", methods=["POST"])
def health_score() -> Any:
    """New Agent 9: Repository Health Scoring Agent — score out of 100."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    framework = framework_details.get("framework", "unknown")
    score_result = calculate_health_score(repo_path, project_type, framework)

    return jsonify({
        "status": "health-scored",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "framework": framework,
        "health_score": score_result,
        "message": (
            f"Repository Health Score: {score_result['total_score']}/100 "
            f"(Grade: {score_result['grade']})"
        ),
    })


# ---------------------------------------------------------------------------
# NEW AGENT 10 — DEPLOYMENT RECOMMENDATION AGENT
# ---------------------------------------------------------------------------

def recommend_deployment(project_type: str, framework: str, is_fullstack: bool) -> dict[str, Any]:
    """Recommend the best deployment strategy based on project characteristics."""
    recommendations: list[dict[str, str]] = []
    primary: str = ""
    instructions: list[str] = []

    if framework == "react":
        primary = "Vercel"
        recommendations.append({
            "platform": "Vercel",
            "reason": "Best-in-class for React/Next.js static and SSR deployments. Zero-config CI/CD.",
            "url": "https://vercel.com",
        })
        recommendations.append({
            "platform": "Netlify",
            "reason": "Excellent CDN and build pipeline for React SPAs with form handling.",
            "url": "https://netlify.com",
        })
        instructions = [
            "1. Push your code to GitHub.",
            "2. Connect the repository to Vercel at https://vercel.com/new.",
            "3. Vercel auto-detects React/Next.js and sets build commands.",
            "4. Set environment variables in the Vercel dashboard.",
            "5. Each push to main triggers a new deployment.",
        ]

    elif framework in ("express", "fastify", "nestjs"):
        primary = "Render"
        recommendations.append({
            "platform": "Render",
            "reason": "Simple Node.js web service deployment with free tier and auto-deploys.",
            "url": "https://render.com",
        })
        recommendations.append({
            "platform": "Railway",
            "reason": "Instant Node.js deployments with built-in database support.",
            "url": "https://railway.app",
        })
        instructions = [
            "1. Create a new Web Service on Render.",
            "2. Connect your GitHub repository.",
            "3. Set Build Command: `npm install`",
            "4. Set Start Command: `npm start`",
            "5. Configure environment variables in Render dashboard.",
        ]

    elif framework in ("flask", "fastapi", "django"):
        primary = "Railway"
        recommendations.append({
            "platform": "Railway",
            "reason": "Excellent Python support with automatic Dockerfile detection and managed databases.",
            "url": "https://railway.app",
        })
        recommendations.append({
            "platform": "Render",
            "reason": "Supports Python web services with Gunicorn. Free tier available.",
            "url": "https://render.com",
        })
        recommendations.append({
            "platform": "Fly.io",
            "reason": "Global edge deployment for Python apps. Good Docker support.",
            "url": "https://fly.io",
        })
        start_cmd = (
            "gunicorn app:app --bind 0.0.0.0:$PORT"
            if framework == "flask"
            else "uvicorn app:app --host 0.0.0.0 --port $PORT"
            if framework == "fastapi"
            else "python manage.py runserver 0.0.0.0:$PORT"
        )
        instructions = [
            "1. Ensure requirements.txt and Dockerfile are present.",
            f"2. Start command: `{start_cmd}`",
            "3. Connect repository to Railway or Render.",
            "4. Set environment variables (DATABASE_URL, SECRET_KEY, etc.).",
            "5. Railway auto-detects Python projects and builds from Dockerfile.",
        ]

    elif framework == "streamlit":
        primary = "Streamlit Community Cloud"
        recommendations.append({
            "platform": "Streamlit Community Cloud",
            "reason": "Purpose-built for Streamlit apps. Free hosting with GitHub integration.",
            "url": "https://share.streamlit.io",
        })
        instructions = [
            "1. Push code to a public (or private) GitHub repository.",
            "2. Go to https://share.streamlit.io and sign in with GitHub.",
            "3. Select your repository and main app file.",
            "4. Configure secrets in Streamlit Cloud settings.",
            "5. Your app is live at a share.streamlit.io URL.",
        ]

    else:
        primary = "Docker (self-hosted)"
        recommendations.append({
            "platform": "Docker + VPS",
            "reason": "Full control via Dockerfile. Deploy to any VPS (DigitalOcean, Hetzner, etc.).",
            "url": "https://www.docker.com",
        })
        instructions = [
            "1. Build Docker image: `docker build -t your-app .`",
            "2. Push to Docker Hub or a private registry.",
            "3. Pull and run on your VPS: `docker run -d -p 80:5000 your-app`",
            "4. Use nginx as a reverse proxy for production.",
        ]

    if is_fullstack:
        recommendations.append({
            "platform": "Docker Compose (full-stack)",
            "reason": "Ideal for full-stack projects combining frontend and backend in a single compose file.",
            "url": "https://docs.docker.com/compose/",
        })

    return {
        "primary_recommendation": primary,
        "recommendations": recommendations,
        "deployment_instructions": instructions,
    }


@app.route("/recommend-deployment", methods=["POST"])
def recommend_deployment_endpoint() -> Any:
    """New Agent 10: Deployment Recommendation Agent — suggest the best deployment platform."""
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400

    ok, reason = validate_github_repo_url(repo_url)
    if not ok:
        return jsonify({"error": reason}), 400

    try:
        repo_path, repo_name, clone_logs = clone_or_update_repo(repo_url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    project_type = detect_project_type(repo_path)
    framework_details = detect_framework_details(repo_path)
    framework = framework_details.get("framework", "unknown")
    is_fullstack = framework_details.get("is_fullstack", False)
    deployment = recommend_deployment(project_type, framework, is_fullstack)

    return jsonify({
        "status": "deployment-recommended",
        "repo": repo_name,
        "clone_logs": clone_logs,
        "project_type": project_type,
        "framework": framework,
        "is_fullstack": is_fullstack,
        "deployment": deployment,
        "message": f"Primary recommendation: {deployment['primary_recommendation']}",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
