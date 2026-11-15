# Google Cloud Platform (GCP) Deployment Guide

This guide fulfills **Course Requirement 11 (Cloud Deployment)** for SurveyOps as documented in `docs/03-REQUIREMENTS.md` and `docs/05-INFRA.md`.

---

## 1. Architecture & Security Invariants

SurveyOps runs as a unified multi-container system orchestrated with **Docker Compose** on a single cloud VM:

```
                            INTERNET / EVALUATOR
                                     │
                    ┌────────────────┴────────────────┐
                    │ Firewall: Only Ports 22 & 8050 │
                    └────────────────┬────────────────┘
                                     │
           ┌─────────────────────────▼──────────────────────────┐
           │ GCP Compute Engine VM (e2-standard-4, Ubuntu)      │
           │                                                    │
           │  ┌──────────────────────────────────────────────┐  │
           │  │ Port 8050: SurveyOps Demo UI Dashboard        │  │
           │  │ (Architecture SVG, Live SSE stream, Inject)  │  │
           │  └──────────────────────┬───────────────────────┘  │
           │                         │ Internal bridge          │
           │  ┌──────────────────────┴───────────────────────┐  │
           │  │ SurveyOps Agents & Services                  │  │
           │  │ • API (:8000, shared-secret auth)            │  │
           │  │ • Runner (:8005, A2A ADK agent)              │  │
           │  │ • Remediator (:8006, A2A CrewAI agent)       │  │
           │  │ • Controller (:8088, internal only)          │  │
           │  │ • SurveyOps Postgres & pgvector (:5434)      │  │
           │  └──────────────────────┬───────────────────────┘  │
           │                         │                          │
           │  ┌──────────────────────▼───────────────────────┐  │
           │  │ Target App Microservices                     │  │
           │  │ • orders (:8001), payment (:8002),           │  │
           │  │   inventory (:8003), auth (:8004)            │  │
           │  │ • Target Postgres (:5432)                    │  │
           │  │ • Prometheus (:9090) & Alertmanager (:9093) │  │
           │  │ • Locust load generator                      │  │
           │  │ • Toxiproxy (Port 8474 loopback only)        │  │
           │  └──────────────────────────────────────────────┘  │
           └────────────────────────────────────────────────────┘
```

### Security & Invariant Checklist
1. **Public Exposure**: Only port `8050` (Demo UI) and port `22` (SSH) are accessible externally.
2. **Ground Truth & Port 8474 Isolation**: Toxiproxy control API (`port 8474`) reveals active injected faults. It is bound strictly to loopback (`127.0.0.1:8474`) and the internal `data_net` Docker network. It is **never** accessible from the public internet or by agent containers.
3. **Secret Isolation**: Secrets (`DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `GITHUB_TOKEN`, `INVESTIGATE_SHARED_SECRET`) live strictly in `.env` on the VM. They are never written to source control or container images.
4. **Token Redaction**: Tokens matching `github_pat_`, `ghp_`, or `gho_` are automatically redacted across all logged outputs, traces, and reports.

---

## 2. VM Sizing & Cost Recommendation

| Specification | Recommendation | Reason |
|---|---|---|
| **Instance Type** | `e2-standard-4` | 4 vCPUs, 16 GB RAM. Comfortably runs all ~17 containers (target services, databases, Prometheus, Locust, and agents) with zero OOM throttling. |
| **Operating System** | Ubuntu 22.04 LTS or 24.04 LTS (x86/64) | Full Docker Engine & Compose plugin support. |
| **Disk** | 40–50 GB Balanced Persistent Disk | Docker container layers, pgvector indices, and log caches. |
| **Estimated Cost** | ~$0.134 / hour (~$3.20 / day) | Fully covered by GCP $300 Free Trial credits or Google Cloud Student credits. |
| **Budget Alternative** | `e2-standard-2` (2 vCPU, 8 GB RAM) + 4 GB swapfile | Lower cost (~$0.067/hr), but image builds take longer. |

---

## 3. Provisioning on GCP

You can deploy using either **Google Cloud Shell / `gcloud` CLI** (recommended) or the **Google Cloud Console Web UI**.

### Option A: Via Google Cloud Shell / `gcloud` CLI

1. **Open Google Cloud Shell** or authenticate your local CLI:
   ```bash
   gcloud auth login
   gcloud config set project <YOUR_PROJECT_ID>
   ```

2. **Create the VPC Firewall Rule** (allows port 22 and port 8050):
   ```bash
   gcloud compute firewall-rules create allow-surveyops \
       --direction=INGRESS \
       --priority=1000 \
       --network=default \
       --action=ALLOW \
       --rules=tcp:22,tcp:8050 \
       --source-ranges=0.0.0.0/0 \
       --target-tags=surveyops-stack \
       --description="Allow SSH and SurveyOps Demo UI only"
   ```

3. **Launch the Compute Engine VM**:
   ```bash
   gcloud compute instances create surveyops-vm \
       --zone=us-central1-a \
       --machine-type=e2-standard-4 \
       --boot-disk-size=50GB \
       --boot-disk-type=pd-balanced \
       --image-family=ubuntu-2204-lts \
       --image-project=ubuntu-os-cloud \
       --tags=surveyops-stack
   ```

4. **SSH into the VM**:
   ```bash
   gcloud compute ssh surveyops-vm --zone=us-central1-a
   ```

---

### Option B: Via Google Cloud Web Console

1. Navigate to **Compute Engine > VM instances > Create instance**.
2. **Name**: `surveyops-vm`
3. **Region / Zone**: e.g. `us-central1` (`us-central1-a`) or `asia-south1`.
4. **Machine configuration**:
   - Series: **E2**
   - Machine type: **e2-standard-4** (4 vCPU, 16 GB memory).
5. **Boot disk**:
   - Click *Change*.
   - Operating system: **Ubuntu**, Version: **Ubuntu 22.04 LTS (x86/64)**.
   - Boot disk size: **50 GB**. Click *Select*.
6. **Networking**:
   - Under *Networking*, add network tag: `surveyops-stack`.
7. **Create the VM**.
8. Navigate to **VPC network > Firewall > Create Firewall Rule**:
   - Name: `allow-surveyops`
   - Target tags: `surveyops-stack`
   - Source IPv4 ranges: `0.0.0.0/0` (or your personal IP `YOUR_IP/32`)
   - Specified protocols and ports: Check **TCP** and enter `22, 8050`.
   - Click *Create*.
9. Click **SSH** next to `surveyops-vm` in the VM instances table.

---

## 4. Bootstrapping the VM with SurveyOps

Once connected to the VM via SSH, run the automated bootstrap:

### 1. Clone the repository
```bash
git clone https://github.com/ayush-prataps/survey-ops-ai.git
cd survey-ops-ai
```

### 2. Configure `.env`
```bash
cp .env.example .env
```
Edit `.env` using `nano .env` and configure your API keys:
```bash
# Model keys (use DeepSeek or Gemini)
DEEPSEEK_API_KEY=your_deepseek_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here

# Target Repo URL and GitHub token (PR rights on target repo)
TARGET_REPO_URL=https://github.com/ayush-prataps/surveyops-target-app.git
GITHUB_TOKEN=your_github_personal_access_token_here

# Generate a strong internal secret
INVESTIGATE_SHARED_SECRET=$(openssl rand -hex 16)
```

### 3. Run the Bootstrap Script
Run the automated bootstrap script to install Docker, configure UFW, build containers, and start the system:
```bash
chmod +x scripts/setup_gcp_vm.sh
bash scripts/setup_gcp_vm.sh
```

The script will:
- Install Docker Engine & Docker Compose plugin
- Configure UFW local firewall (allowing only ports 22 and 8050)
- Build and spin up the 17 containers via `docker compose`
- Wait for all healthchecks to pass
- Output the public Demo UI link: `http://<VM_EXTERNAL_IP>:8050`

---

## 5. Verification & Live Demo on GCP

### Accessing the Demo UI
Open your browser and navigate to:
```
http://<VM_EXTERNAL_IP>:8050
```
You will see:
- Interactive **Architecture Map** with network zones (`target_net`, `data_net`, `ops_net`, `controller_net`).
- **Phase Narration** banner and live SSE event stream.
- Active Prometheus metrics mini-strip (`payment p99`, `DB pool utilization`).

### Running a Live Fault Trial
1. In the Demo UI header, click **Inject**.
2. Watch the live workflow:
   - Toxiproxy introduces latency on Postgres port `5433`.
   - Payment DB pool saturates; latency jumps.
   - Prometheus detects high p99 latency; Alertmanager fires webhook to `/investigate`.
   - **Diagnostician (ADK LoopAgent)** formulates hypotheses, requests experiments via A2A, and disconfirms rivals.
   - Once verified, Diagnostician dispatches A2A diagnosis to **Remediator (CrewAI)**.
   - Remediator clones `surveyops-target-app`, writes behavioral concurrency test, verifies fail-before, patches QueuePool configuration, verifies pass-after, and opens a GitHub PR.
3. Click **Reveal** to score the trial double-blind against ground truth.
4. Click **Reset** to clear the fault and reset the database state.

### Viewing Grafana (Human Dashboard)
To inspect Grafana telemetry securely without exposing port 3000 to the public:
```bash
ssh -L 3000:localhost:3000 user@<VM_EXTERNAL_IP>
```
Then open `http://localhost:3000` in your local browser (login: `admin` / `admin`).

---

## 6. Resource Management & Teardown

To avoid unnecessary cloud billing when not running evaluations or demonstrations:

- **Pause the VM** (charges stop for compute, minimal charges for disk):
  ```bash
  gcloud compute instances stop surveyops-vm --zone=us-central1-a
  ```
- **Resume the VM**:
  ```bash
  gcloud compute instances start surveyops-vm --zone=us-central1-a
  ```
- **Delete when coursework is complete**:
  ```bash
  gcloud compute instances delete surveyops-vm --zone=us-central1-a
  gcloud compute firewall-rules delete allow-surveyops
  ```

