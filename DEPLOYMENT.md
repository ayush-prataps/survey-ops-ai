# SurveyOps: Cloud Deployment Guide

This guide documents the production cloud deployment of **SurveyOps** on **Google Cloud Platform (GCP)** to fulfill **Course Requirement 11 (Cloud Deployment)** for CSE3101 Agentic AI at BML Munjal University.

---

## 1. Deployment Architecture

SurveyOps runs as a unified multi-container system orchestrated with **Docker Compose** on an isolated GCP Compute Engine Virtual Machine:

```
                            INTERNET / EVALUATOR
                                     │
                    ┌────────────────┴────────────────┐
                    │ Firewall: Only Ports 22 & 8050 │
                    └────────────────┬────────────────┘
                                     │
           ┌─────────────────────────▼──────────────────────────┐
           │ GCP Compute Engine VM (e2-standard-4, Ubuntu 22.04)│
           │                                                    │
           │  ┌──────────────────────────────────────────────┐  │
           │  │ Port 8050: SurveyOps Demo UI Dashboard        │  │
           │  │ (Architecture SVG, Live SSE stream, Inject)  │  │
           │  └──────────────────────┬───────────────────────┘  │
           │                         │ Internal bridge          │
           │  ┌──────────────────────┴───────────────────────┐  │
           │  │ SurveyOps Multi-Agent System                 │  │
           │  │ • API (:8000, shared-secret auth)            │  │
           │  │ • Runner (:8005, A2A ADK agent)              │  │
           │  │ • Remediator (:8006, A2A CrewAI agent)       │  │
           │  │ • Controller (:8088, internal only)          │  │
           │  │ • SurveyOps Postgres & pgvector (:5434)      │  │
           │  └──────────────────────┬───────────────────────┘  │
           │                         │                          │
           │  ┌──────────────────────▼───────────────────────┐  │
           │  │ Target Application Under Diagnosis           │  │
           │  │ • Orders (:8001), Payment (:8002),           │  │
           │  │   Inventory (:8003), Auth (:8004)            │  │
           │  │ • Target Postgres (:5432)                    │  │
           │  │ • Prometheus (:9090) & Alertmanager (:9093) │  │
           │  │ • Locust load generator                      │  │
           │  │ • Toxiproxy (Port 8474 loopback only)        │  │
           │  └──────────────────────────────────────────────┘  │
           └────────────────────────────────────────────────────┘
```

---

## 2. Security & Architectural Invariants

The deployment enforces all non-negotiable security invariants from `AGENTS.md`:

1. **Public Attack Surface Minimization**:
   * **Only port 8050 (Demo UI) and port 22 (SSH) are open externally.**
   * Target microservices (`8001`–`8004`), internal agent A2A ports (`8005`, `8006`), internal API (`8000`), controller (`8088`), Prometheus (`9090`), and databases (`5432`, `5434`) are isolated behind Docker internal bridge networks and the host firewall.
2. **Double-Blind Port 8474 Isolation**:
   * Toxiproxy's control API (`port 8474`) reveals active ground-truth faults. It is strictly bound to loopback (`127.0.0.1`) and the internal `data_net` Docker network. No agent container can resolve or reach port 8474.
3. **Secret Isolation & Token Redaction**:
   * API keys (`DEEPSEEK_API_KEY`, `GEMINI_API_KEY`) and `GITHUB_TOKEN` live exclusively in `.env` on the host VM and are passed as environment variables.
   * Remediator receives `GITHUB_TOKEN` only inside its isolated container without access to the host file system.
   * All logged outputs, traces, and reports automatically redact tokens matching `github_pat_`, `ghp_`, or `gho_`.
4. **Remediator Permissions**:
   * Remediator only has permission to branch, commit, and open a Pull Request. It has zero merge authority. Human review and merge are strictly required.

---

## 3. Recommended Cloud Sizing

| Component | Specification | Rationale |
|---|---|---|
| **Cloud Provider** | Google Cloud Platform (GCP) | Native Google ADK support, high-speed regional networking. |
| **Instance Type** | `e2-standard-4` | 4 vCPUs, 16 GB RAM. Easily hosts all 17 containers simultaneously without CPU starvation or OOM kills. |
| **Operating System**| Ubuntu 22.04 LTS (x86_64) | Standard long-term support with Docker Engine and Compose plugin. |
| **Boot Disk** | 50 GB SSD (`pd-ssd`) | Fast I/O for container builds, layer caching, and Postgres/pgvector storage. |
| **Estimated Cost** | ~$0.134 / hour (~$3.20 / day) | Fully covered by Google Cloud $300 Free Trial / Student credits. |

---

## 4. Step-by-Step Cloud Provisioning

### Step 1: Open Google Cloud Shell
Go to [Google Cloud Console](https://console.cloud.google.com/) and click **Activate Cloud Shell** (`>_`) in the top navigation bar.

### Step 2: Create the VPC Firewall Rule
Run in Cloud Shell to allow SSH and public access to port 8050 only:
```bash
gcloud compute firewall-rules create allow-surveyops \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22,tcp:8050 \
    --target-tags=surveyops-host \
    --description="Allow SSH and SurveyOps Demo UI only"
```

### Step 3: Launch the VM Instance
Create the `e2-standard-4` instance with the `surveyops-host` tag:
```bash
gcloud compute instances create surveyops-vm \
    --zone=us-central1-a \
    --machine-type=e2-standard-4 \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=50GB \
    --boot-disk-type=pd-ssd \
    --tags=surveyops-host
```

### Step 4: Connect to the VM
```bash
gcloud compute ssh surveyops-vm --zone=us-central1-a
```

---

## 5. Automated Setup & Stack Launch

Once connected to the VM via SSH (`photu2075@surveyops-vm:~$`), run:

```bash
# 1. Clone the repository
git clone https://github.com/ayush-prataps/survey-ops-ai.git
cd survey-ops-ai

# 2. Configure environment variables
cp .env.example .env
nano .env
```

### Configuring `.env`:
Set the required model keys and GitHub token:
```bash
# Model keys (Gemini or DeepSeek)
GEMINI_API_KEY=your_gemini_api_key_here
DEEPSEEK_API_KEY=your_deepseek_api_key_here

# Remediator target repository and GitHub Token
TARGET_REPO_URL=https://github.com/ayush-prataps/surveyops-target-app.git
GITHUB_TOKEN=your_github_personal_access_token_here
```
*(Save with `Ctrl+O` then `Enter`, exit with `Ctrl+X`)*.

### 3. Run the Bootstrap Script:
```bash
bash scripts/setup_gcp_vm.sh
```

The bootstrap script automatically:
1. Installs Docker CE, containerd, and Docker Compose v2.
2. Configures UFW host firewall (allowing only ports 22 and 8050).
3. Synchronizes `.env` with `infra/.env`.
4. Builds all custom images and spins up the 17-container stack.
5. Polls each container's health check until verified.

---

## 6. Accessing & Demonstrating the System

### Find the Public IP:
Run on the VM:
```bash
curl -s ifconfig.me
```

Open in your browser:
```
http://<EXTERNAL_IP>:8050
```

### Live Demo Flow:
1. **Explore Architecture**: The browser displays an SVG topology showing all target services, the load generator, Prometheus, Toxiproxy, and the SurveyOps agents.
2. **Inject Fault**: Click **Inject** on the Demo UI. This introduces synthetic latency on the payment database connection via Toxiproxy.
3. **Observe Autonomous Diagnostic Loop**:
   * Prometheus alerts fire on p99 latency (`HighP99Latency`).
   * Alertmanager delivers webhook to SurveyOps API (`/investigate`).
   * **Diagnostician Agent (ADK LoopAgent)** queries metrics and tests hypotheses against the **Runner Agent (ADK over A2A)**.
   * Once diagnosed with confidence $\ge 0.80$, Diagnostician dispatches diagnosis to **Remediator Agent (CrewAI over A2A)**.
   * Remediator localizes the code issue, executes tests, and opens a GitHub Pull Request proposing the fix.
4. **Post-Run Scoring**: Click **Reveal** to score the diagnosis against ground truth and inspect the generated incident report.
5. **Clean Reset**: Click **Reset** to purge faults and restore baseline traffic.

---

## 7. Cost & Resource Management

When you are done with a demo session:

* **Stop the VM** (stops compute billing, preserves disk and images):
  ```bash
  gcloud compute instances stop surveyops-vm --zone=us-central1-a
  ```
* **Start the VM again later**:
  ```bash
  gcloud compute instances start surveyops-vm --zone=us-central1-a
  ```
  *(All containers will automatically start back up via `restart: unless-stopped`)*.
* **Permanently Delete the VM** (at end of semester / after grading):
  ```bash
  gcloud compute instances delete surveyops-vm --zone=us-central1-a
  gcloud compute firewall-rules delete allow-surveyops
  ```
