# Cloud Agent Orchestrator

## Overview

An autonomous multi-agent system that analyzes GitHub repositories, detects issues, repairs missing configurations, generates infrastructure files, and prepares projects for production deployment.

Supports **React** (Vite, CRA, Next.js), **Node.js** (Express, Fastify, NestJS), and **Python** (Flask, FastAPI, Django, ML) projects.

## Tech Stack

- **Language**: Python 3.11
- **Framework**: Flask
- **Containerization**: Docker + Gunicorn

## Installation

```bash
pip install -r requirements.txt
```

## Running the Application

```bash
python app.py
# or for production
gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 app:app
```

## Environment Setup

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

See `.env.example` for available configuration options.

## Docker Usage

```bash
# Build the image
docker build -t cloud-agent .

# Run the container
docker run -p 5000:5000 -v /var/run/docker.sock:/var/run/docker.sock cloud-agent
```

> **Note**: Mount the Docker socket (`/var/run/docker.sock`) so the agent can build and run containers for analyzed repositories.

## API Usage

### Analyze a Repository (Agent 1)

```http
POST /analyze
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

### Run Full Orchestration Pipeline (Agents 1–8)

```http
POST /orchestrate
Content-Type: application/json

{
  "repo_url": "https://github.com/user/repo",
  "env_values": {},
  "auto_fill_env": true,
  "force_rebuild": false
}
```

### Code Quality Review (Agent 6)

```http
POST /review-repo
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo", "quick_mode": true, "max_findings": 200 }
```

### Generate README (Agent 9)

```http
POST /generate-readme
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo", "write_to_file": false }
```

### Improvement Report (Agent 10)

```http
POST /improvement-report
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

### Deploy to Platform (Post-orchestration)

```http
POST /deploy-platform
Content-Type: application/json

{
  "session_id": "<session_id from orchestrate>",
  "user_satisfied": true,
  "platform": "render"
}
```

Supported platforms: `render`, `railway`, `flyio`

### Health Check

```http
GET /health
```

### New Advanced Agent Endpoints

#### Self-Fixing Repository Agent

```http
POST /fix-repo
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

#### Automated Pull Request Agent

```http
POST /prepare-pr
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

#### Architecture Visualization Agent

```http
POST /visualize-architecture
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

#### Performance Analysis Agent

```http
POST /analyze-performance
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

#### Security Analysis Agent

```http
POST /analyze-security
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

#### Dependency Cleanup Agent

```http
POST /cleanup-dependencies
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

#### Test Generation Agent

```http
POST /generate-tests
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo", "force": false }
```

#### API Discovery & Documentation Agent

```http
POST /discover-api
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo", "write_docs": false }
```

#### Repository Health Scoring Agent

```http
POST /health-score
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

#### Deployment Recommendation Agent

```http
POST /recommend-deployment
Content-Type: application/json

{ "repo_url": "https://github.com/user/repo" }
```

## Agent Architecture

| Agent | Endpoint / Function | Responsibility |
|-------|---------------------|----------------|
| 1 | `/analyze` | Repository clone & structure analysis |
| 2 | `validate_dependencies` | Dependency file validation |
| 3 | `detect_env_keys` / `write_env_example` | Environment configuration |
| 4 | `detect_framework_details` | Exact project type detection |
| 5 | `validate_build` | Build validation |
| 6 | `/review-repo` | Code quality analysis |
| 7 | `generate_dockerfile` | Docker image generation |
| 8 | `docker_build_and_run` | Docker build validation |
| 9 | `/generate-readme` | README generation |
| 10 | `/improvement-report` | Repository improvement report |

### New Advanced Agents

| Agent | Endpoint | Responsibility |
|-------|----------|----------------|
| Fix Repo | `/fix-repo` | Self-Fixing Repository Agent — auto-apply common fixes |
| Prepare PR | `/prepare-pr` | Automated Pull Request Agent — generate PR metadata |
| Visualize Architecture | `/visualize-architecture` | Architecture Visualization Agent — Mermaid diagrams |
| Performance Analysis | `/analyze-performance` | Performance Analysis Agent — detect anti-patterns |
| Security Analysis | `/analyze-security` | Security Analysis Agent — deep security inspection |
| Cleanup Dependencies | `/cleanup-dependencies` | Dependency Cleanup Agent — find unused packages |
| Generate Tests | `/generate-tests` | Test Generation Agent — scaffold starter tests |
| Discover API | `/discover-api` | API Discovery & Documentation Agent |
| Health Score | `/health-score` | Repository Health Scoring Agent — score out of 100 |
| Recommend Deployment | `/recommend-deployment` | Deployment Recommendation Agent |

## Build Instructions

```bash
# Install dependencies
pip install -r requirements.txt

# Run in development mode
python app.py

# Run in production mode
gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 app:app
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/improvement`)
3. Commit your changes
4. Push and open a Pull Request

## License

See [LICENSE](LICENSE) for details.
